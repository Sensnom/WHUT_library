"""Bounded interval subdivision and short-lived checkpoints for seat scans."""

import json
from math import ceil
from time import time

from .availability import merge_free_chunk, seat_row, time_chunks

CACHE_MAX_AGE = 120


class ScanCacheError(RuntimeError):
    pass


class ScanCache:
    def __init__(self, path, save, *, resume=False):
        self.path = path
        self.save = save
        self.records = {"version": 1, "chunks": {}, "seats": {}}
        if resume:
            try:
                records = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise ScanCacheError("没有可继续的查询记录，请先开始查询。") from None
            if (not isinstance(records, dict) or records.get("version") != 1 or
                    not isinstance(records.get("chunks"), dict) or not isinstance(records.get("seats"), dict)):
                raise ScanCacheError("查询记录已失效，请重新开始查询。")
            self.records = records
        else:
            self.flush()

    def get(self, group, key):
        record = self.records[group].get(key)
        if not isinstance(record, dict) or not isinstance(record.get("at"), (int, float)):
            return None
        if 0 <= time() - record["at"] <= CACHE_MAX_AGE:
            return record.get("data")
        return None

    def put(self, group, key, data):
        self.records[group][key] = {"at": time(), "data": data}
        self.flush()

    def flush(self):
        now = time()
        for group in ("chunks", "seats"):
            self.records[group] = {
                key: record for key, record in self.records[group].items()
                if isinstance(record, dict) and isinstance(record.get("at"), (int, float))
                and 0 <= now - record["at"] <= CACHE_MAX_AGE
            }
        try:
            self.save(self.path, self.records)
        except OSError:
            raise ScanCacheError("无法保存查询进度，请检查项目目录的写入权限。") from None


class IntervalScan:
    """Probe wide intervals with at most two extra requests over a uniform grid."""

    def __init__(self, places, start, end, step, *, prioritize_start=False):
        self.step = step
        self.rows = {
            str(place["id"]): {**seat_row(place), "position": position, "time_step": step}
            for position, place in enumerate(places)
        }
        queryable = []
        for seat_id, row in self.rows.items():
            if not row.get("state"):
                row.update(slots=[], state="sampling")
                queryable.append(seat_id)
        self.pending = [(start, end, queryable)] if start < end and queryable else []
        if self.pending and prioritize_start and start + step < end:
            self.pending = [(start, start + step, queryable), (start + step, end, queryable)]
        self.used = 0
        cells = ceil(max(0, end - start) / step)
        self.budget = cells + min(2, max(0, cells - 1))
        self.current = None
        self.finish_rows()

    def cells(self, start, end):
        return ceil((end - start) / self.step)

    def next_interval(self, cutoff):
        # Trim again on every request, including when a resumed scan crosses a time boundary.
        for row in self.rows.values():
            if isinstance(row.get("slots"), list):
                row["slots"] = [(max(start, cutoff), end) for start, end in row["slots"] if end > cutoff]
        self.pending = [(max(start, cutoff), end, ids)
                        for start, end, ids in self.pending if end > cutoff]
        while self.pending:
            start, end, ids = self.pending[0]
            worst = sum(self.cells(a, b) for a, b, _ in self.pending)
            if end - start > self.step and self.used + worst >= self.budget:
                self.pending[:1] = [(a, b, ids) for a, b in time_chunks(start, end, self.step)]
                continue
            self.current = start, end, ids
            return start, end
        self.finish_rows()
        return None

    def accept(self, data):
        start, end, ids = self.current
        self.current = None
        self.pending.pop(0)
        self.used += 1
        unresolved = []
        for seat_id in ids:
            row = self.rows[seat_id]
            status = data.get(seat_id)
            if not isinstance(status, dict) or "status" not in status:
                row.update(slots=None, state="failed")
            elif status["status"] == "FREE":
                merge_free_chunk(row["slots"], start, end)
            elif end - start > self.step:
                unresolved.append(seat_id)

        if unresolved:
            middle = start + (self.cells(start, end) // 2) * self.step
            self.pending[:0] = [(start, middle, unresolved), (middle, end, unresolved)]
        self.pending = [
            (a, b, [seat_id for seat_id in ids if self.rows[seat_id].get("state") != "failed"])
            for a, b, ids in self.pending
            if any(self.rows[seat_id].get("state") != "failed" for seat_id in ids)
        ]
        self.finish_rows()

    def finish_rows(self):
        unresolved = {seat_id for _, _, ids in self.pending for seat_id in ids}
        for seat_id, row in self.rows.items():
            if row.get("state") == "sampling" and seat_id not in unresolved:
                del row["state"]
