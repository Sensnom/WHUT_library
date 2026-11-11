import sys
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from webui.jobs import JobRunner, booking_date, reservation_options


WORKER = """
import json, sys
print(json.dumps({'type': 'confirm', 'seat': {'label': 'A03'}}), flush=True)
reply = json.loads(sys.stdin.readline())
print(json.dumps({'type': 'log', 'text': reply['decision']}), flush=True)
"""


class WebUITests(unittest.TestCase):
    def test_relative_booking_dates_use_wuhan_time(self):
        with patch("webui.jobs.datetime") as clock:
            clock.now.side_effect = lambda tz: datetime(
                2026, 11, 10, 16, 5, tzinfo=timezone.utc,
            ).astimezone(tz)
            self.assertEqual(booking_date("today"), "2026-11-11")
            self.assertEqual(booking_date("tomorrow"), "2026-11-12")
        self.assertEqual(reservation_options({"date": "tomorrow"})["date"], "tomorrow")

    def test_defaults_match_cli(self):
        options = reservation_options({"floor": 7, "date": "2026-11-10", "start": "09:30"})
        self.assertEqual(options["floor"], 7)
        self.assertTrue(options["auto_confirm"])
        self.assertEqual(options["max_attempts"], 0)
        self.assertEqual(options["interval"], 3)
        self.assertEqual(options["session"], "whut")

    def test_accepts_cli_options_without_extra_caps(self):
        options = reservation_options({
            "duration": 1500, "interval": 4000, "max_attempts": 20000, "session": "study",
        })
        self.assertEqual(options["duration"], 1500)
        self.assertEqual(options["interval"], 4000)
        self.assertEqual(options["max_attempts"], 20000)
        self.assertEqual(options["session"], "study")
        self.assertEqual(reservation_options({"interval": 0})["interval"], 0)
        self.assertEqual(reservation_options({"once": True, "max_attempts": 30})["max_attempts"], 1)
        self.assertFalse(reservation_options({"auto_confirm": False})["auto_confirm"])

    def test_rejects_invalid_web_form_data(self):
        for invalid in ({"floor": 5}, {"interval": float("inf")}, {"date": "2026-02-30"}, {"start": "24:00"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                reservation_options(invalid)

    def test_allows_one_job_and_rejects_stale_confirmations(self):
        runner = JobRunner()
        self.addCleanup(runner.close)
        runner.start([sys.executable, "-u", "-c", WORKER])
        pending = self.wait_for(runner, lambda state: state["pending"] is not None)
        with self.assertRaises(ValueError):
            runner.start([sys.executable, "-c", "pass"])
        with self.assertRaises(ValueError):
            runner.confirm("stale", "accept")
        runner.confirm(pending["pending"]["id"], "skip")
        finished = self.wait_for(runner, lambda state: state["status"] == "finished")
        self.assertIn("skip", finished["logs"])

    def test_can_stop_a_waiting_worker(self):
        runner = JobRunner()
        self.addCleanup(runner.close)
        started = runner.start([sys.executable, "-u", "-c", WORKER])
        self.wait_for(runner, lambda state: state["pending"] is not None)
        runner.stop(started["id"])
        stopped = self.wait_for(runner, lambda state: state["status"] == "stopped")
        self.assertIsNone(stopped["pending"])

    def test_availability_timeline_replaces_pending_seat_by_id(self):
        worker = """
import json
def emit(event, **fields):
    print(json.dumps({'type': event, **fields}))
emit('availability', date='2026-11-10', stage='inventory')
emit('room', room={'id': 'a', 'total': 2})
emit('seats', room_id='a', seats=[
    {'id':'1', 'label':'001', 'state':'pending'}, {'id':'2', 'label':'002', 'state':'pending'}
])
emit('availability', date='2026-11-10', stage='timelines')
emit('seats', room_id='a', seats=[{'id':'2', 'label':'002', 'slots':[[540, 660]]}])
"""
        runner = JobRunner()
        self.addCleanup(runner.close)
        runner.start([sys.executable, "-u", "-c", worker], kind="availability")
        self.wait_for(runner, lambda state: state["status"] == "finished")
        result = runner.availability_snapshot()
        self.assertEqual(result["stage"], "timelines")
        first, second = result["rooms"][0]["seats"]
        self.assertEqual(first["state"], "pending")
        self.assertEqual(second["slots"], [[540, 660]])

    def wait_for(self, runner, predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            state = runner.snapshot()
            if predicate(state):
                return state
            time.sleep(0.02)
        self.fail(f"worker did not reach expected state: {runner.snapshot()}")


if __name__ == "__main__":
    unittest.main()
