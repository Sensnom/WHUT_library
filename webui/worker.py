"""Run the existing CLI workflow and forward logs and confirmation events."""

import json
import re
import runpy
import sys
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from queue import Empty, Queue
from threading import Lock
from threading import Thread
from time import monotonic

from playwright.sync_api import Error as BrowserError

from .jobs import ROOT, booking_date
from .official import OfficialAvailability, OfficialQueryError
from .scan import ScanCache, ScanCacheError

ANSI = re.compile(r"\x1b\[[0-9;]*m")
IDLE_BROWSER_SECONDS = 90


def serve_availability(seat, options, emit):
    login = seat["SeatLogin"](options.session)
    step = getattr(options, "time_step", 0)
    filename = f"{login.cache_path.stem}-{options.date}-{options.floor}-{step}.scan.json"
    path = login.cache_path.with_name(filename)
    resume = getattr(options, "resume", False) or (getattr(options, "refresh", False) and path.exists())
    cache = ScanCache(path, seat["save_auth_state"], resume=resume)
    official = OfficialAvailability(seat, options.date, cache=cache)
    incoming = Queue()

    def read_commands():
        try:
            for line in sys.stdin:
                incoming.put(json.loads(line))
        finally:
            incoming.put(None)

    Thread(target=read_commands, daemon=True).start()
    with login.browser_page(on_page=official.attach) as (page, _):
        official.collect(options, emit)
        emit("complete")
        deadline = monotonic() + IDLE_BROWSER_SECONDS
        while monotonic() < deadline:
            try:
                command = incoming.get_nowait()
            except Empty:
                page.wait_for_timeout(100)
                continue
            if command is None:
                return
            next_options = Namespace(**command["options"])
            next_options.date = booking_date(next_options.date)
            if next_options.date != official.day:
                raise OfficialQueryError("查询日期已变化，请重新开始查询。")
            more = official.refresh(next_options, emit)
            emit("complete")
            if not more:
                return
            deadline = monotonic() + IDLE_BROWSER_SECONDS


def main():
    kind, payload = sys.argv[1:]
    options = Namespace(**json.loads(payload))
    if kind != "login":
        options.date = booking_date(options.date)
    seat = runpy.run_path(str(ROOT / "whut-seat.py"))
    output = sys.stdout
    output_lock = Lock()

    def emit(event_type, **data):
        with output_lock:
            print(json.dumps({"type": event_type, **data}), file=output, flush=True)

    class LogStream:
        def __init__(self):
            self.pending = ""

        def write(self, text):
            self.pending += ANSI.sub("", text).replace("\r", "\n")
            while "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                if line.strip():
                    emit("log", text=line.strip())
            return len(text)

        def flush(self):
            if self.pending.strip():
                emit("log", text=self.pending.strip())
            self.pending = ""

    def confirm(candidate, day):
        emit("confirm", seat={
            "label": str(candidate["seat_label"]),
            "name": str(candidate["seat_name"]),
            "room": str(candidate["room_name"]),
            "date": day,
            "start": seat["to_str"](candidate["start"]),
            "end": seat["to_str"](candidate["end"]),
        })
        reply = sys.stdin.readline()
        return bool(reply) and json.loads(reply).get("decision") == "accept"

    stream = LogStream()
    with redirect_stdout(stream), redirect_stderr(stream):
        try:
            if kind == "login":
                seat["SeatLogin"](options.session).authenticate()
                emit("result", result="verified")
            elif kind == "availability":
                serve_availability(seat, options, emit)
            else:
                booked = seat["run"](options.session, options, confirm=confirm)
                emit("result", result="booked" if booked else "no_booking")
        except (seat["LoginError"], seat["SeatRateLimitError"], seat["CaptchaError"], OfficialQueryError, ScanCacheError) as exc:
            print(f"❌ {exc}")
            return 1
        except BrowserError:
            print("❌ 官网页面交互失败，本轮查询已停止。")
            return 1
        finally:
            stream.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
