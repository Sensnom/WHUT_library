"""Read seat data produced by normal interactions with the official page."""

from collections import deque
from copy import deepcopy
from datetime import datetime
from time import monotonic
from urllib.parse import urlsplit

from playwright.sync_api import Error, TimeoutError

from .availability import replace_free_window, seat_order, seat_row, seat_slots, time_chunks
from .jobs import LIBRARY_TZ
from .scan import IntervalScan


class OfficialQueryError(RuntimeError):
    pass


class OfficialRateLimitError(OfficialQueryError):
    pass


CANVAS = ".seatLayoutCanvasContainer canvas.upper-canvas:visible"
TIMELINE_INTERVAL = 0.5
TIMELINE_CONCURRENCY = 3
BULK_INTERVAL = 3.0
CANVAS_DATA = """
canvas => {
    let design;
    for (let node = canvas; node && !design; node = node.parentElement) {
        design = node.__vue__?.seatPreview?.design;
    }
    if (!design) return null;
    const seats = design.getObjects().filter(object => object.type === "labeledImage" && object.seat?.id);
    return seats.map(({seat}) => ({
        id: String(seat.id), label: seat.label || seat.name || String(seat.id),
        lock: seat.lock, scene: seat.scene,
    }));
}
"""

REQUEST_TIMELINE = """
(canvas, place) => {
    let view;
    for (let node = canvas; node; node = node.parentElement) {
        if (node.__vue__?.seatPreview?.design) {
            view = node.__vue__;
            break;
        }
    }
    if (!view || typeof view.getTimelineFun !== "function") return false;
    view.seat = place;
    view.getTimelineFun();
    return true;
}
"""

REQUEST_CHUNK = """
(canvas, query) => {
    for (let node = canvas; node; node = node.parentElement) {
        const view = node.__vue__;
        const method = view?.$options?.methods?.freeSeatIdsDurationFun;
        if (view?.seatPreview?.design && typeof method === "function") {
            const context = Object.create(view);
            Object.defineProperty(context, "$route", {value:{
                ...view.$route,
                query:{...view.$route.query, id:query.room, makeDate:query.day},
            }});
            method.call(context, {begin:query.start, end:query.end}, view.seatPreview);
            return true;
        }
    }
    return false;
}
"""


class OfficialAvailability:
    def __init__(self, seat, day, *, cache=None):
        self.seat = seat
        self.day = day
        self.responses = deque(maxlen=16)
        self.room_response = None
        self.next_timeline = 0
        self.next_bulk = 0
        self.duration_mode = True
        self.cache = cache
        self.work = []
        self.rows = {}
        self.refresh_queue = deque()
        self.bounds = None
        self.room_modes = {}

    def publish_seats(self, room_id, rows, emit):
        self.rows.setdefault(room_id, {}).update((row["id"], deepcopy(row)) for row in rows)
        emit("seats", room_id=room_id, seats=rows)

    def attach(self, page):
        self.page = page
        page.on("response", self.record)

    def record(self, response):
        url = urlsplit(response.url)
        if (f"{url.scheme}://{url.netloc}" == self.seat["APP_ORIGIN"] and
                url.path.startswith("/jsq/static/frontApi/res/") and response.request.method == "POST"):
            self.responses.append(response)
            if self.matches(response, ("findRoomDuration", "findRoomSlice"), self.seat.get("BUILDING_ID", "")):
                self.room_response = response

    def matches(self, response, methods, identifier):
        url = urlsplit(response.url)
        return (f"{url.scheme}://{url.netloc}" == self.seat["APP_ORIGIN"] and any(
            url.path == f"/jsq/static/frontApi/res/{method}/{identifier}/{self.day}" for method in methods
        ))

    def data(self, response, label):
        try:
            result = response.json()
        except (Error, ValueError):
            raise OfficialQueryError(f"官网未返回有效的{label}。") from None
        message = str(result.get("message", "")) if isinstance(result, dict) else ""
        if response.status == 429 or "过于频繁" in message:
            raise OfficialRateLimitError(f"官网读取{label}时提示访问过于频繁，本轮查询已停止。")
        if not isinstance(result, dict) or not result.get("status"):
            raise OfficialQueryError(f"官网{label}读取失败。" + (f" {message[:120]}" if message else ""))
        return result.get("data")

    def dismiss_notice(self):
        close = self.page.get_by_role("button", name="close 系统公告")
        if close.is_visible():
            try:
                close.click(timeout=2500)
                close.wait_for(state="hidden", timeout=4000)
            except TimeoutError:
                if close.is_visible():
                    raise OfficialQueryError("官网系统公告未能关闭。") from None

    def room_list(self):
        self.dismiss_notice()
        calendar = self.page.locator(".myCalendar .day")
        day_number = str(int(self.day[-2:]))
        chosen = calendar.filter(has=self.page.get_by_text(day_number, exact=True))
        try:
            chosen.wait_for(state="visible", timeout=8000)
        except TimeoutError:
            raise OfficialQueryError("官网日期列表未加载。") from None
        if chosen.locator(".disable").count():
            raise OfficialQueryError("官网尚未开放这一天的座位。")

        def is_room_list(response):
            return self.matches(response, ("findRoomDuration", "findRoomSlice"), self.seat["BUILDING_ID"])

        if not chosen.locator(".check").count():
            try:
                with self.page.expect_response(is_room_list, timeout=12000) as response:
                    chosen.click()
                response = response.value
            except TimeoutError:
                raise OfficialQueryError("官网切换日期后未返回房间列表。") from None
        else:
            response = next((response for response in reversed(self.responses) if is_room_list(response)),
                            self.room_response)
            if response is None:
                try:
                    response = self.page.wait_for_event("response", predicate=is_room_list, timeout=12000)
                except TimeoutError:
                    raise OfficialQueryError("官网未返回所选日期的房间列表。") from None
        data = self.data(response, "房间列表")
        if not isinstance(data, dict) or not isinstance(data.get("pageList"), list):
            raise OfficialQueryError("官网房间列表格式有变化。")
        return data["pageList"]

    def open_room(self, room):
        self.dismiss_notice()
        try:
            with self.page.expect_response(lambda response: self.matches(
                response, ("freeSeatIdsDuration", "freeSeatIdsSlice"), room["id"],
            ), timeout=12000) as response:
                self.page.get_by_text(room["name"], exact=True).first.click()
            bulk = self.data(response.value, "座位概览")
            self.duration_mode = "/freeSeatIdsDuration/" in urlsplit(response.value.url).path
            self.room_modes[str(room["id"])] = self.duration_mode
            self.next_bulk = monotonic() + BULK_INTERVAL
            self.canvas = self.page.locator(CANVAS).first
            self.canvas.wait_for(state="visible", timeout=10000)
            self.page.wait_for_function(
                f"""selector => {{
                    const canvas = document.querySelector(selector);
                    return canvas && ({CANVAS_DATA})(canvas)?.length > 0;
                }}""",
                arg=".seatLayoutCanvasContainer canvas.upper-canvas", timeout=10000,
            )
            layout = self.canvas.evaluate(CANVAS_DATA)
        except TimeoutError:
            raise OfficialQueryError("官网座位图未加载。") from None
        if not isinstance(bulk, dict) or not layout:
            raise OfficialQueryError("官网座位图未提供座位。")
        return sorted([
            {**place, **(bulk.get(place["id"]) or {}), "id": place["id"]}
            for place in layout
        ], key=seat_order)

    def check_limit(self):
        if self.page.get_by_text("访问过于频繁", exact=False).first.is_visible():
            raise OfficialRateLimitError("官网提示访问过于频繁，本轮查询已停止。")

    def read_timeline(self, place):
        return next(self.read_timelines([(0, place)]))[1]

    def read_timelines(self, places, *, use_cache=True):
        uncached = []
        for position, place in places:
            cached = self.cache.get("seats", str(place["id"])) if self.cache and use_cache else None
            if isinstance(cached, dict) and isinstance(cached.get("slots"), list):
                yield position, {**cached, "position": position}
            else:
                uncached.append((position, place))
        remaining = iter(uncached)
        pending = {}
        exhausted = False
        while pending or not exhausted:
            self.check_limit()
            for response in tuple(self.responses):
                seat_id = next((seat_id for seat_id in pending
                                if self.matches(response, ("getTimeLine",), seat_id)), None)
                if seat_id is None:
                    continue
                self.responses.remove(response)
                position, place, _ = pending.pop(seat_id)
                try:
                    data = self.data(response, "座位时间轴")
                except OfficialRateLimitError:
                    raise
                except OfficialQueryError:
                    data = None
                row = seat_slots(place, data, self.seat["parse_timeline_range"])
                if self.cache and row["slots"] is not None:
                    self.cache.put("seats", seat_id, row)
                yield position, row

            now = monotonic()
            for seat_id, (position, place, deadline) in tuple(pending.items()):
                if now >= deadline:
                    del pending[seat_id]
                    yield position, seat_row(place)

            if not exhausted and len(pending) < TIMELINE_CONCURRENCY and now >= self.next_timeline:
                item = next(remaining, None)
                if item is None:
                    exhausted = True
                else:
                    position, place = item
                    seat_id = str(place["id"])
                    pending[seat_id] = (position, place, now + 12)
                    self.next_timeline = now + TIMELINE_INTERVAL
                    if not self.canvas.evaluate(REQUEST_TIMELINE, {"id": seat_id}):
                        raise OfficialQueryError("官网时间轴查询入口有变化，本轮查询已停止。")
                    continue

            if pending or not exhausted:
                self.page.wait_for_timeout(50)

    def current_cutoff(self, step):
        now = datetime.now(LIBRARY_TZ)
        if self.day < now.date().isoformat():
            return 1440
        if self.day > now.date().isoformat():
            return 0
        minute = now.hour * 60 + now.minute + 1
        return ((minute + step - 1) // step) * step

    def query_chunk(self, room, start, end, step, *, use_cache=True):
        key = f"{room['id']}:{start}:{end}"
        cached = self.cache.get("chunks", key) if self.cache and use_cache else None
        if isinstance(cached, dict):
            return cached
        delay = self.next_bulk - monotonic()
        if delay > 0:
            self.page.wait_for_timeout(delay * 1000)
        if start < self.current_cutoff(step):
            return None
        self.check_limit()
        try:
            with self.page.expect_response(lambda response: self.matches(
                response, ("freeSeatIdsDuration",), room["id"],
            ), timeout=12000) as response:
                self.next_bulk = monotonic() + BULK_INTERVAL
                if not self.canvas.evaluate(REQUEST_CHUNK, {
                    "room": str(room["id"]), "day": self.day, "start": start, "end": end,
                }):
                    raise OfficialQueryError("官网批量查询入口有变化，本轮查询已停止。")
        except TimeoutError:
            raise OfficialQueryError("官网批量时段查询超时，本轮查询已停止。") from None
        label = f"批量时段 {start // 60:02d}:{start % 60:02d}–{end // 60:02d}:{end % 60:02d}"
        data = self.data(response.value, label)
        if not isinstance(data, dict):
            raise OfficialQueryError("官网批量时段格式有变化，本轮查询已停止。")
        # Cache only the fields required to prove a whole interval is free.
        statuses = {str(key): {"status": value["status"]} for key, value in data.items()
                    if isinstance(value, dict) and "status" in value}
        if self.cache:
            self.cache.put("chunks", key, statuses)
        return statuses

    def sample_room(self, room, plan, emit):
        if not self.room_modes.get(str(room["id"]), self.duration_mode):
            raise OfficialQueryError("该阅览室采用固定场次，请选择精确查询。")
        interval = plan.next_interval(self.current_cutoff(plan.step))
        while interval is not None:
            start, end = interval
            data = self.query_chunk(room, start, end, plan.step)
            if data is not None:
                plan.accept(data)
                self.publish_seats(str(room["id"]), list(plan.rows.values()), emit)
            interval = plan.next_interval(self.current_cutoff(plan.step))
        self.publish_seats(str(room["id"]), list(plan.rows.values()), emit)

    def opening_hours(self):
        if self.bounds is not None:
            return self.bounds
        bounds = self.page.evaluate("""id => {
            const info = JSON.parse(sessionStorage.getItem('jsq_p-systemInfo') || 'null');
            const hours = info?.buildSeTime?.[id];
            return hours ? {open:hours.openTime, close:hours.closeTime} : null;
        }""", self.seat["BUILDING_ID"])
        if not isinstance(bounds, dict) or not all(type(bounds.get(key)) is int for key in ("open", "close")):
            raise OfficialQueryError("官网开放时间未加载，请选择逐座精确查询。")
        start, end = bounds["open"], bounds["close"]
        if not 0 <= start < end <= 1440:
            raise OfficialQueryError("官网开放时间有变化，请选择逐座精确查询。")
        self.bounds = start, end
        return self.bounds

    def sample_all(self, work, step, emit):
        start, end = self.opening_hours()
        start = max(start, self.current_cutoff(step))
        start = ((start + step - 1) // step) * step
        emit("availability", date=self.day, stage="sampling", time_step=step)
        plans = [(room, IntervalScan(places, start, end, step, prioritize_start=True))
                 for room, places in work]
        while plans:
            remaining = []
            for room, plan in plans:
                if not self.room_modes.get(str(room["id"]), self.duration_mode):
                    raise OfficialQueryError("该阅览室采用固定场次，请选择精确查询。")
                interval = plan.next_interval(self.current_cutoff(step))
                if interval is None:
                    self.publish_seats(str(room["id"]), list(plan.rows.values()), emit)
                    continue
                a, b = interval
                data = self.query_chunk(room, a, b, step)
                if data is not None:
                    plan.accept(data)
                    self.publish_seats(str(room["id"]), list(plan.rows.values()), emit)
                remaining.append((room, plan))
            plans = remaining

    def collect(self, options, emit):
        step = getattr(options, "time_step", 0)
        emit("availability", date=self.day, stage="inventory", time_step=step)
        rooms = self.room_list()
        floor = self.seat["FLOOR_NAMES"].get(options.floor)
        rooms = [room for room in rooms if not floor or room.get("floorName") == floor]
        if not rooms:
            raise OfficialQueryError("官网没有返回该楼层的阅览室。")
        work = []
        for index, room in enumerate(rooms):
            if index:
                self.page.go_back()
                self.room_list()
            places = self.open_room(room)
            room_id = str(room["id"])
            expected = int(room.get("seatTotal") or 0)
            emit("room", room={
                "id": room_id, "name": room["name"], "floor": room["floorName"],
                "total": max(expected, len(places)), "partial": bool(expected and len(places) < expected),
            })
            rows = []
            for position, place in enumerate(places):
                row = {**seat_row(place), "position": position}
                if not row.get("state"):
                    row["state"] = "pending"
                    row["overview_free"] = place.get("status") == "FREE" or place.get("afterFree") is True
                rows.append(row)
            self.publish_seats(room_id, rows, emit)
            work.append((room, places))
        self.work = work
        if step:
            self.sample_all(work, step, emit)
            return
        emit("availability", date=self.day, stage="timelines")
        # The page's timeline method only needs the observed seat ID and date.
        # Reuse its current view for all rooms without reloading their overviews.
        for room, places in work:
            consecutive_failures = 0
            queryable = [(position, place) for position, place in enumerate(places)
                         if not seat_row(place).get("state")]
            for position, row in self.read_timelines(queryable):
                self.publish_seats(str(room["id"]), [{**row, "position": position}], emit)
                consecutive_failures = consecutive_failures + 1 if row["slots"] is None else 0
                if consecutive_failures >= 3:
                    raise OfficialQueryError("官网连续三个座位的时间轴无法读取，本轮查询已停止。")

    def refresh(self, options, emit):
        step = getattr(options, "time_step", 0)
        opened, closed = self.opening_hours()
        cutoff = max(opened, self.current_cutoff(step or 1))
        if cutoff >= closed:
            emit("availability", date=self.day, stage="refresh", time_step=step, closed=True)
            return False
        if not self.refresh_queue:
            queues = []
            for room, places in self.work:
                if step:
                    queues.append(deque((room, places, start, end)
                                        for start, end in time_chunks(cutoff, closed, step)))
                else:
                    queryable = [(position, place) for position, place in enumerate(places)
                                 if not seat_row(place).get("state")]
                    queues.append(deque((room, queryable[offset:offset + TIMELINE_CONCURRENCY])
                                        for offset in range(0, len(queryable), TIMELINE_CONCURRENCY)))
            while any(queues):
                for queue in queues:
                    if queue:
                        self.refresh_queue.append(queue.popleft())
        emit("availability", date=self.day, stage="refresh", time_step=step)
        if not self.refresh_queue:
            emit("availability", date=self.day, stage="refresh", time_step=step, closed=True)
            return False
        if not step:
            room, places = self.refresh_queue.popleft()
            for position, row in self.read_timelines(places, use_cache=False):
                self.publish_seats(str(room["id"]), [{**row, "position": position}], emit)
            return True
        while self.refresh_queue:
            room, places, start, end = self.refresh_queue.popleft()
            if start < self.current_cutoff(step):
                continue
            data = self.query_chunk(room, start, end, step, use_cache=False)
            if data is None:
                continue
            rows = []
            for position, place in enumerate(places):
                row = deepcopy(self.rows[str(room["id"])][str(place["id"])])
                if row.get("state") in ("locked", "onsite"):
                    continue
                status = data.get(str(place["id"]))
                if not isinstance(status, dict) or "status" not in status:
                    raise OfficialQueryError("官网返回的座位状态不完整，刷新已停止。")
                row["slots"] = replace_free_window(row.get("slots") or [], start, end,
                                                   status["status"] == "FREE")
                row["slots"] = [(max(a, cutoff), b) for a, b in row["slots"] if b > cutoff]
                if row.get("state") != "failed":
                    row.pop("state", None)
                rows.append({**row, "position": position})
            self.publish_seats(str(room["id"]), rows, emit)
            return True
        return True
