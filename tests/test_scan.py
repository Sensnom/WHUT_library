import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from webui.official import OfficialAvailability
from webui.scan import IntervalScan, ScanCache, ScanCacheError


def save(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


class IntervalScanTests(unittest.TestCase):
    def test_nearest_window_first_still_finds_all_transitions_within_budget(self):
        for mask in range(16):
            plan = IntervalScan([{'id': 'a'}], 480, 600, 30, prioritize_start=True)
            self.assertEqual(plan.next_interval(480), (480, 510))
            while plan.current is not None:
                start, end, _ = plan.current
                queried = sum(1 << cell for cell in range((start - 480) // 30, (end - 480) // 30))
                plan.accept({'a': {'status': 'FREE' if mask & queried == queried else 'BOOKED'}})
                plan.next_interval(480)
            found = {cell for a, b in plan.rows['a']['slots']
                     for cell in range((a - 480) // 30, (b - 480) // 30)}
            self.assertEqual(found, {cell for cell in range(4) if mask & (1 << cell)})
            self.assertLessEqual(plan.used, plan.budget)

    def test_multiple_transitions_and_request_budget_across_all_four_cell_patterns(self):
        for first in range(16):
            for second in range(16):
                masks = {"a": first, "b": second}
                plan = IntervalScan([{"id": key, "label": key} for key in masks], 0, 120, 30)
                while (interval := plan.next_interval(0)) is not None:
                    start, end = interval
                    queried = sum(1 << cell for cell in range(start // 30, end // 30))
                    plan.accept({key: {"status": "FREE" if mask & queried == queried else "BOOKED"}
                                 for key, mask in masks.items()})
                self.assertLessEqual(plan.used, plan.budget)
                for key, mask in masks.items():
                    found = {cell for a, b in plan.rows[key]["slots"] for cell in range(a // 30, b // 30)}
                    self.assertEqual(found, {cell for cell in range(4) if mask & (1 << cell)})
                    self.assertNotIn("state", plan.rows[key])

    def test_wholly_free_interval_finishes_in_one_query(self):
        plan = IntervalScan([{"id": "a", "label": "001"}], 480, 1350, 30)
        self.assertEqual(plan.next_interval(480), (480, 1350))
        plan.accept({"a": {"status": "FREE"}})
        self.assertIsNone(plan.next_interval(480))
        self.assertEqual(plan.used, 1)
        self.assertEqual(plan.rows["a"]["slots"], [(480, 1350)])

    def test_resumed_queue_drops_past_cells(self):
        plan = IntervalScan([{"id": "a", "label": "001"}], 0, 120, 30)
        plan.next_interval(0)
        plan.accept({"a": {"status": "BOOKED"}})
        while (interval := plan.next_interval(60)) is not None:
            self.assertGreaterEqual(interval[0], 60)
            plan.accept({"a": {"status": "FREE"}})
        self.assertEqual(plan.rows["a"]["slots"], [(60, 120)])


class ScanCacheTests(unittest.TestCase):
    def test_interrupted_scan_replays_completed_intervals_and_queries_the_rest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.json"
            successful = []

            def configure(cache, interrupt):
                official = OfficialAvailability({}, "2026-11-10", cache=cache)
                official.page = MagicMock()
                official.canvas = Mock()
                official.check_limit = Mock()
                official.current_cutoff = Mock(return_value=480)

                def data(*_):
                    if interrupt and len(successful) == 2:
                        raise RuntimeError("interrupted before saving this response")
                    query = official.canvas.evaluate.call_args.args[1]
                    start, end = query["start"], query["end"]
                    successful.append((start, end))
                    return {"a": {"status": "FREE" if 510 <= start and end <= 570 else "BOOKED"}}

                official.data = Mock(side_effect=data)
                return official

            cache = ScanCache(path, save)
            first = configure(cache, True)
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                first.sample_room({"id": "room"}, IntervalScan([{"id": "a"}], 480, 600, 30), Mock())
            completed = list(successful)
            resumed = configure(ScanCache(path, save, resume=True), False)
            plan = IntervalScan([{"id": "a"}], 480, 600, 30)
            resumed.sample_room({"id": "room"}, plan, Mock())
            self.assertEqual(plan.rows["a"]["slots"], [(510, 570)])
            for interval in completed:
                self.assertEqual(successful.count(interval), 1)

    def test_resume_reuses_saved_chunk_without_a_browser_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.json"
            cache = ScanCache(path, save)
            data = {"a": {"status": "FREE"}}
            cache.put("chunks", "room:480:600", data)
            resumed = ScanCache(path, save, resume=True)
            official = OfficialAvailability({}, "2026-11-10", cache=resumed)
            official.page = Mock()
            official.canvas = Mock()
            self.assertEqual(official.query_chunk({"id": "room"}, 480, 600, 30), data)
            official.canvas.evaluate.assert_not_called()
            official.page.wait_for_timeout.assert_not_called()

    def test_expired_results_are_requeried_and_new_scan_discards_old_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.json"
            with patch("webui.scan.time", return_value=1000):
                cache = ScanCache(path, save)
                cache.put("seats", "a", {"id": "a", "slots": []})
            with patch("webui.scan.time", return_value=1121):
                resumed = ScanCache(path, save, resume=True)
                self.assertIsNone(resumed.get("seats", "a"))
            new = ScanCache(path, save)
            self.assertIsNone(new.get("seats", "a"))

    def test_resume_without_a_checkpoint_reports_missing_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ScanCacheError, "没有可继续"):
                ScanCache(Path(directory) / "scan.json", save, resume=True)
