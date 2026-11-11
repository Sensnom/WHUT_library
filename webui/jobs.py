"""Validate WebUI parameters and manage one reservation worker at a time."""

import json
import math
import re
import subprocess
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
LIBRARY_TZ = timezone(timedelta(hours=8))


def booking_date(day):
    if day in (None, "today", "tomorrow"):
        today = datetime.now(LIBRARY_TZ).date()
        return (today + timedelta(days=day == "tomorrow")).isoformat()
    return day


def reservation_options(data):
    if not isinstance(data, dict):
        raise ValueError("预约条件格式有误。")
    floor = data.get("floor", 0)
    day = data.get("date") or None
    start = data.get("start") or None
    duration = data.get("duration", 0)
    attempts = data.get("max_attempts", 0)
    interval = data.get("interval", 3)
    auto_confirm = data.get("auto_confirm", True)
    once = data.get("once", False)
    session = data.get("session", "whut")
    if type(floor) is not int or floor not in (0, 6, 7):
        raise ValueError("请选择全部楼层、六楼或七楼。")
    if day not in (None, "today", "tomorrow"):
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("请选择有效日期。") from None
    if start is not None and (
        not isinstance(start, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start)
    ):
        raise ValueError("开始时间须为 24 小时制的 HH:MM。")
    if type(duration) is not int:
        raise ValueError("预约时长须为整数分钟。")
    if type(attempts) is not int or attempts < 0:
        raise ValueError("尝试次数须为非负整数，0 表示持续查找。")
    try:
        valid_interval = type(interval) in (int, float) and math.isfinite(interval)
    except OverflowError:
        valid_interval = False
    if not valid_interval:
        raise ValueError("请输入有效的刷新间隔。")
    if type(auto_confirm) is not bool:
        raise ValueError("请选择有效的预约确认方式。")
    if type(once) is not bool or not isinstance(session, str):
        raise ValueError("请检查尝试模式和会话名称。")
    return {
        "floor": floor, "date": day, "start": start, "duration": duration,
        "max_attempts": 1 if once else attempts, "interval": interval,
        "auto_confirm": auto_confirm, "session": session,
    }


class JobRunner:
    def __init__(self):
        self.lock = Lock()
        self.process = None
        self.id = None
        self.kind = None
        self.status = "idle"
        self.pending = None
        self.result = None
        self.logs = deque(maxlen=160)
        self.availability = None
        self.availability_version = 0
        self.availability_signature = None

    @staticmethod
    def worker_options(command):
        if len(command) != 6 or command[1:5] != ["-u", "-m", "webui.worker", "availability"]:
            return None
        try:
            options = json.loads(command[-1])
        except ValueError:
            return None
        return options if isinstance(options, dict) else None

    def retire_idle_worker(self):
        process = self.process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self.process = None

    def _snapshot(self):
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "pending": self.pending, "result": self.result, "logs": list(self.logs),
            "availability_version": self.availability_version,
        }

    def snapshot(self):
        with self.lock:
            return self._snapshot()

    def availability_snapshot(self):
        with self.lock:
            if self.availability is None:
                return None
            return {
                **self.availability,
                "rooms": [{**room, "seats": list(room["seats"].values())}
                          for room in self.availability["rooms"].values()],
            }

    def start(self, command, kind="reservation"):
        with self.lock:
            options = self.worker_options(command) if kind == "availability" else None
            signature = tuple(options.get(key) for key in (
                "date", "floor", "time_step", "session", "config_version",
            )) if options is not None else None
            refresh = options is not None and options.get("refresh", False)
            if self.process is not None:
                if self.status != "finished":
                    raise ValueError("已有任务在运行。")
                if refresh and signature == self.availability_signature:
                    try:
                        self.process.stdin.write(json.dumps({"options": options}) + "\n")
                        self.process.stdin.flush()
                    except (OSError, ValueError):
                        self.retire_idle_worker()
                    else:
                        self.id = uuid4().hex
                        self.status = "running"
                        self.availability["status"] = "running"
                        self.availability.pop("error", None)
                        self.availability_version += 1
                        return self._snapshot()
                else:
                    self.retire_idle_worker()
            preserve = refresh and signature == self.availability_signature and self.availability is not None
            process = subprocess.Popen(
                command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            )
            self.process = process
            self.id = uuid4().hex
            self.kind = kind
            self.status = "running"
            self.pending = self.result = None
            self.logs.clear()
            if kind == "availability":
                if not preserve:
                    self.availability = {"date": None, "status": self.status, "rooms": {}}
                else:
                    self.availability["status"] = self.status
                    self.availability.pop("error", None)
                self.availability_signature = signature
                self.availability_version += 1
            Thread(target=self._read_worker, args=(process,), daemon=True).start()
            return self._snapshot()

    def _read_worker(self, process):
        try:
            for line in process.stdout:
                with self.lock:
                    if self.process is process:
                        self._record(line)
        finally:
            code = process.wait()
            with self.lock:
                process.stdout.close()
                process.stdin.close()
                if self.process is not process:
                    return
                self.process = None
                self.pending = None
                if self.status == "stopping":
                    self.status = "stopped"
                else:
                    self.status = "finished" if code == 0 else "failed"
                if self.kind == "availability":
                    self.availability["status"] = self.status
                    if self.status == "failed" and self.logs:
                        self.availability["error"] = self.logs[-1]
                    self.availability_version += 1

    def _record(self, line):
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            self.logs.append(line.strip()[:1000])
        elif event.get("type") == "log":
            self.logs.append(event["text"][:1000])
        elif event.get("type") == "confirm" and self.status == "running":
            self.pending = {"id": uuid4().hex, "seat": event["seat"]}
        elif event.get("type") == "result":
            self.result = event["result"]
        elif event.get("type") == "complete" and self.kind == "availability":
            self.status = "finished"
            self.availability["status"] = "finished"
            self.availability_version += 1
        elif self.kind == "availability":
            if event.get("type") == "availability":
                self.availability["date"] = event["date"]
                self.availability["stage"] = event.get("stage", "timelines")
                for key in ("time_step", "sample_done", "sample_total", "closed"):
                    if key in event:
                        self.availability[key] = event[key]
            elif event.get("type") == "room":
                room = event["room"]
                self.availability["rooms"].setdefault(room["id"], {"seats": {}}).update(room)
            elif event.get("type") == "seats":
                rows = self.availability["rooms"][event["room_id"]]["seats"]
                rows.update((row.get("id") or row["label"], row) for row in event["seats"])
            else:
                return
            self.availability_version += 1

    def confirm(self, confirmation_id, decision):
        with self.lock:
            if (
                self.status != "running" or not self.pending or
                self.pending["id"] != confirmation_id or decision not in ("accept", "skip")
            ):
                raise ValueError("确认请求已失效，请查看当前任务。")
            try:
                self.process.stdin.write(json.dumps({"decision": decision}) + "\n")
                self.process.stdin.flush()
            except OSError:
                raise ValueError("任务已结束。") from None
            self.pending = None
            return self._snapshot()

    def _stop(self):
        self.status = "stopping"
        self.pending = None
        self.process.terminate()

    def stop(self, job_id):
        with self.lock:
            if self.status != "running" or self.id != job_id:
                raise ValueError("当前任务已结束或已更换。")
            self._stop()
            return self._snapshot()

    def close(self):
        with self.lock:
            process = self.process
            if process is not None and self.status in ("running", "finished"):
                self._stop()
        if process is not None:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
