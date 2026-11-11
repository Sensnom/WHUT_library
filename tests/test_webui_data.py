import tempfile
import unittest
from copy import deepcopy
from argparse import Namespace
from pathlib import Path
from threading import Thread
from unittest.mock import MagicMock, Mock, patch

from dotenv import dotenv_values
from playwright.sync_api import expect, sync_playwright

from webui.__main__ import make_server
from webui.availability import merge_free_chunk, seat_slots, time_chunks
from webui.official import OfficialAvailability, OfficialQueryError
from webui.configuration import Configuration
from webui.jobs import JobRunner
from webui.scan import IntervalScan


class ConfigurationTests(unittest.TestCase):
    def test_round_trips_defaults_and_keeps_password_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# preserved\nOTHER_SETTING=leave-alone\nWHUT_PASSWORD='fixture secret'\n")
            config = Configuration(path)
            result = config.save({
                "username": "test-user",
                "defaults": {"floor": 7, "duration": 120, "once": True, "max_attempts": 42},
            })
            self.assertTrue(result["password_configured"])
            self.assertNotIn("fixture secret", str(result))
            self.assertEqual(result["defaults"]["max_attempts"], 42)
            self.assertTrue(result["defaults"]["once"])
            self.assertEqual(config.public(), result)
            self.assertIn("OTHER_SETTING=leave-alone", path.read_text())
            self.assertIn("# preserved", path.read_text())

    def test_invalid_defaults_do_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("WHUT_DURATION=120\n")
            config = Configuration(path)
            with self.assertRaises(ValueError):
                config.save({"username": "x", "defaults": {"start": "26:00"}})
            self.assertEqual(path.read_text(), "WHUT_DURATION=120\n")

    def test_password_with_quotes_and_backslashes_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            password = "a'\"\\\\$b\\"
            result = Configuration(path).save({"username": "x", "password": password, "defaults": {}})
            self.assertNotIn("password", result)
            self.assertEqual(dotenv_values(path, interpolate=False)["WHUT_PASSWORD"], password)

    def test_browser_round_trips_date_and_start_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("WHUT_START=09:30\n", encoding="utf-8")
            with make_server(0, JobRunner(), Configuration(path)) as server:
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as driver, driver.chromium.launch() as browser:
                        page = browser.new_page()
                        page.goto(f"http://127.0.0.1:{server.server_port}/")
                        reservation = page.locator("#reservation-form")
                        expect(reservation.get_by_label("开始时间")).to_have_value("09:30")
                        expect(reservation.get_by_label("今天")).to_be_checked()
                        page.get_by_role("link", name="配置管理").click()
                        settings = page.locator("#settings-form")
                        settings.get_by_label("明天").check()
                        expect(settings.get_by_role("option", name="最早可预约")).to_have_count(1)
                        settings.get_by_label("开始时间").select_option("custom")
                        settings.get_by_role("textbox", name="自定义时间").fill("09:15")
                        settings.get_by_role("button", name="保存配置").click()
                        expect(settings.get_by_text("已保存", exact=True)).to_be_visible()
                        self.assertEqual(dotenv_values(path)["WHUT_DATE"], "tomorrow")
                        page.goto(f"http://127.0.0.1:{server.server_port}/")
                        expect(reservation.get_by_label("明天")).to_be_checked()
                        expect(reservation.get_by_role("textbox", name="自定义时间")).to_have_value("09:15")
                        page.get_by_role("link", name="空闲时段").click()
                        expect(page.locator("#availability-form").get_by_label("明天")).to_be_checked()
                finally:
                    server.shutdown()
                    thread.join()


class AvailabilityTests(unittest.TestCase):
    def test_room_list_remains_available_after_many_sampling_responses(self):
        origin = "https://zw.whut.edu.cn"
        official = OfficialAvailability({"APP_ORIGIN": origin, "BUILDING_ID": "building"}, "2026-11-10")
        room_response = Mock(url=origin + "/jsq/static/frontApi/res/findRoomDuration/building/2026-11-10")
        room_response.request.method = "POST"
        official.record(room_response)
        for _ in range(20):
            response = Mock(url=origin + "/jsq/static/frontApi/res/freeSeatIdsDuration/room/2026-11-10")
            response.request.method = "POST"
            official.record(response)
        official.page = Mock()
        official.dismiss_notice = Mock()
        selected = official.page.locator.return_value.filter.return_value
        selected.locator.side_effect = lambda selector: Mock(count=Mock(return_value=selector == ".check"))
        official.data = Mock(return_value={"pageList": [{"id": "room"}]})
        self.assertEqual(official.room_list(), [{"id": "room"}])
        official.data.assert_called_once_with(room_response, "房间列表")
        official.page.wait_for_event.assert_not_called()

    def test_time_grid_preserves_partial_cells_and_does_not_merge_across_busy_gaps(self):
        self.assertEqual(list(time_chunks(487, 555, 30)), [(487, 510), (510, 540), (540, 555)])
        slots = []
        for start, end in ((487, 510), (510, 540), (570, 600)):
            merge_free_chunk(slots, start, end)
        self.assertEqual(slots, [(487, 540), (570, 600)])

    def test_adaptive_scan_merges_multiple_free_runs_for_each_seat(self):
        official = OfficialAvailability({}, "2026-11-10")
        official.current_cutoff = Mock(return_value=0)
        windows = {"a": (540, 600), "b": (570, 630)}

        def query(room, start, end, step):
            return {key: {"status": "FREE" if a <= start and end <= b else "BOOKED",
                          "afterFree": True} for key, (a, b) in windows.items()}

        official.query_chunk = Mock(side_effect=query)
        plan = IntervalScan([{"id": "a", "label": "001"}, {"id": "b", "label": "002"}],
                            540, 630, 30)
        emitted = []

        def emit(event, **data):
            emitted.append(deepcopy(data["seats"]))

        official.sample_room({"id": "room"}, plan, emit)
        self.assertEqual(emitted[0][0]["state"], "sampling")
        self.assertEqual(emitted[-1][0]["slots"], [(540, 600)])
        self.assertEqual(emitted[-1][1]["slots"], [(570, 630)])
        self.assertNotIn("state", emitted[-1][0])
        self.assertLessEqual(official.query_chunk.call_count, plan.budget)

    def test_missing_bulk_seat_is_unknown_instead_of_free_or_busy(self):
        official = OfficialAvailability({}, "2026-11-10")
        official.current_cutoff = Mock(return_value=0)
        official.query_chunk = Mock(return_value={})
        plan = IntervalScan([{"id": "a", "label": "001"}], 540, 570, 30)
        official.sample_room({"id": "room"}, plan, Mock())
        self.assertIsNone(plan.rows["a"]["slots"])
        self.assertEqual(plan.rows["a"]["state"], "failed")

    def test_three_queries_can_finish_out_of_order_without_mixing_seats(self):
        origin = "https://zw.whut.edu.cn"
        with sync_playwright() as driver, driver.chromium.launch() as browser:
            page = browser.new_page()
            page.route(origin + "/", lambda route: route.fulfill(
                content_type="text/html; charset=utf-8",
                body="""<canvas></canvas><script>
                    window.active = 0;
                    window.peak = 0;
                    document.querySelector('canvas').__vue__ = {
                        seatPreview: {design: {}},
                        getTimelineFun() {
                            const id = this.seat.id;
                            window.peak = Math.max(window.peak, ++window.active);
                            setTimeout(async () => {
                                const response = await fetch(
                                    '/jsq/static/frontApi/res/getTimeLine/' + id + '/2026-11-10',
                                    {method: 'POST'});
                                await response.json();
                                --window.active;
                            }, {a: 240, b: 40, c: 100, d: 20, e: 20}[id]);
                        },
                    };
                </script>""",
            ))

            def response(route):
                seat_id = route.request.url.split("/")[-2]
                end = 10 + ord(seat_id) - ord("a")
                route.fulfill(json={"status": True, "data": {"freeList": [
                    {"label": f"09:00-{end:02d}:00"}
                ]}})

            page.route(origin + "/jsq/static/frontApi/res/**", response)
            page.goto(origin)
            parse = lambda label: (540, int(label.split("-")[1][:2]) * 60)
            official = OfficialAvailability({"APP_ORIGIN": origin, "parse_timeline_range": parse},
                                             "2026-11-10")
            official.attach(page)
            official.canvas = page.locator("canvas")
            places = [(index, {"id": key, "label": key}) for index, key in enumerate("abcde")]
            with patch("webui.official.TIMELINE_INTERVAL", 0):
                rows = list(official.read_timelines(places))

            self.assertEqual(page.evaluate("window.peak"), 3)
            self.assertEqual(rows[0][1]["id"], "b")
            self.assertCountEqual([position for position, _ in rows], range(5))
            for position, row in rows:
                self.assertEqual(row["id"], "abcde"[position])
                self.assertEqual(row["slots"], [(540, (10 + position) * 60)])

    def test_timeline_uses_one_official_page_request_per_seat(self):
        origin = "https://zw.whut.edu.cn"
        day = "2026-11-10"
        with sync_playwright() as driver, driver.chromium.launch() as browser:
            page = browser.new_page()
            page.route(origin + "/", lambda route: route.fulfill(
                content_type="text/html; charset=utf-8",
                body="""<div class="seatLayoutCanvasContainer"><canvas class="upper-canvas"></canvas></div>
                <script>
                    const view = {
                        seatPreview: {design: {getObjects: () => [
                            {type: 'labeledImage', seat: {id:'a'}},
                        ]}},
                        getTimelineFun() {
                            fetch('/jsq/static/frontApi/res/getTimeLine/' + this.seat.id + '/2026-11-10',
                                  {method:'POST'}).then(response => response.json());
                        },
                    };
                    document.querySelector('canvas').__vue__ = view;
                </script>""",
            ))
            page.route(origin + "/jsq/static/frontApi/res/**", lambda route: route.fulfill(
                json={"status": True, "data": {"freeList": [{"label": "09:00-11:00"}]}},
            ))
            official = OfficialAvailability({
                "APP_ORIGIN": origin,
                "parse_timeline_range": lambda _: (540, 660),
            }, day)
            official.attach(page)
            page.goto(origin)
            official.canvas = page.locator("canvas")
            requests = []
            page.on("request", lambda request: requests.append(request.url))
            for seat_id in ("a", "b"):
                self.assertEqual(official.read_timeline({"id": seat_id, "label": seat_id})["slots"],
                                 [(540, 660)])
            self.assertEqual(requests, [
                f"{origin}/jsq/static/frontApi/res/getTimeLine/a/{day}",
                f"{origin}/jsq/static/frontApi/res/getTimeLine/b/{day}",
            ])

    def test_official_lists_all_rooms_then_reads_all_queryable_seats(self):
        official = OfficialAvailability({"FLOOR_NAMES": {6: "六楼"}}, "2026-11-10")
        official.page = Mock()
        rooms = [{"id": key, "name": key, "floorName": "六楼", "seatTotal": 2} for key in ("a", "b")]
        official.room_list = Mock(return_value=rooms)
        official.open_room = Mock(side_effect=lambda room: [
            {"id": room["id"] + "1", "label": "001", "status": "FREE"},
            {"id": room["id"] + "2", "label": "002", "lock": room["id"] == "b"},
        ])
        order = []

        def timelines(places):
            for position, place in places:
                order.append(("timeline", place["id"]))
                slots = None if place["id"] == "a1" else [(540, 660)]
                yield position, {"id": place["id"], "label": place["label"], "slots": slots}

        def emit(event, **payload):
            if event == "seats" and payload["seats"][0].get("state") == "pending":
                order.append(("inventory", payload["room_id"]))

        official.read_timelines = Mock(side_effect=timelines)
        official.collect(Namespace(floor=6), emit)
        self.assertEqual(order, [
            ("inventory", "a"), ("inventory", "b"), ("timeline", "a1"),
            ("timeline", "a2"), ("timeline", "b1"),
        ])
        self.assertEqual(official.page.go_back.call_count, 1)

    def test_official_rate_limit_in_body_is_reported(self):
        official = OfficialAvailability({}, "2026-11-10")
        response = Mock(status=200)
        response.json.return_value = {"status": False, "code": 500, "message": "访问过于频繁 请稍后再试"}
        with self.assertRaisesRegex(OfficialQueryError, "本轮查询已停止"):
            official.data(response, "座位时间轴")

    def test_seat_timeline_distinguishes_no_slots_from_failed_query(self):
        timeline = {"freeList": [{"label": "13:15-15:00"}, {"label": "09:00-11:00"}]}
        parse = lambda label: tuple(int(h) * 60 + int(m) for h, m in
                                    (part.split(":") for part in label.split("-")))
        seat = {"id": "x", "label": "003"}
        self.assertEqual(seat_slots(seat, {"freeList": []}, parse)["slots"], [])
        self.assertIsNone(seat_slots(seat, None, parse)["slots"])
        self.assertEqual(seat_slots(seat, timeline, parse)["slots"],
                         [(540, 660), (795, 900)])
