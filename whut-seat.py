#!/usr/bin/env python3
"""
武汉理工大学南湖图书馆 — 座位预约抢座脚本
跨平台：Linux / Windows / macOS
依赖：requests、playwright、python-dotenv (Python 3.9+)
浏览器：python -m playwright install chromium

默认行为：
  - 不带参数运行会进入交互式配置
  - 直接回车会选择默认项
  - 默认楼层：全部
  - 默认日期：今天
  - 默认开始：现在开始（自动对齐到下一个 30 分钟档）
  - 默认时长：最长可预约时间
  - 默认刷新：3 秒 ± 1 秒随机抖动
  - 默认次数：无限循环，直到抢到或手动 Ctrl+C 停止
  - 自动避开座位号：187,188,189,190,191,192,193,194
  - 默认找到符合条件的座位后自动确认预约；可关闭自动确认，改为手动确认

用法示例:
  python whut-seat.py                         # 交互式配置
  python whut-seat.py --floor 6               # 仅六楼，今天，现在开始，最长时长，无限尝试
  python whut-seat.py --floor 7               # 仅七楼
  python whut-seat.py -f 7 -s 09:00 -t 120    # 七楼，从09:00开始，预约120分钟
  python whut-seat.py -f 0 -t 180             # 全部楼层，从现在开始，预约180分钟
  python whut-seat.py -d 2026-06-17           # 指定日期
  python whut-seat.py --max-attempts 50       # 最多尝试50次
  python whut-seat.py --once                  # 只试一次
"""

import argparse
import hashlib
import json
import os
import random
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from threading import Lock, local
from urllib.parse import quote, urlsplit

from reservation_captcha import CaptchaAttemptError, CaptchaError, ReservationCaptcha

# ============================================================
# 配置常量
# ============================================================

APP_ORIGIN = "https://zw.whut.edu.cn"
API_BASE = f"{APP_ORIGIN}/jsq/static"
BUILDING_ID = "2009170845461745664"  # 南湖馆
APP_URL = f"{APP_ORIGIN}/jsq-v/"

# 楼层 → {name, floorId}
FLOOR_NAMES = {6: "六楼", 7: "七楼"}

# ============================================================
# ANSI 颜色辅助
# ============================================================

def red(s):    return f"\033[1;31m{s}\033[0m"
def green(s):  return f"\033[1;32m{s}\033[0m"
def yellow(s): return f"\033[1;33m{s}\033[0m"
def cyan(s):   return f"\033[1;36m{s}\033[0m"
def bold(s):   return f"\033[1m{s}\033[0m"


# ============================================================
# 统一身份认证
# ============================================================

PROJECT_DIR = Path(__file__).resolve().parent
SSO_ORIGIN = "https://one.whut.edu.cn"
SSO_PATH = "/tpass/login"

LOGIN_STATE = r"""
({appOrigin, ssoOrigin, ssoPath, submitted}) => {
    if (location.origin === appOrigin) {
        const token = sessionStorage.getItem('jsq_p-token');
        if (token && token.length > 5) return {token};
    }
    if (location.origin !== ssoOrigin) return null;

    const text = document.body?.innerText || '';
    if (/滑块|人机验证|请输入验证码|短信验证|二次验证|安全验证|拖动滑块/.test(text))
        return {verification: true};
    if (submitted && /账号或密码|用户名或密码|密码错误|密码不正确|账号锁定|登录失败/.test(text))
        return {rejected: true};
    if (!submitted && location.pathname === ssoPath &&
        document.querySelector('#un') && document.querySelector('#pd') &&
        document.querySelector('#index_login_btn'))
        return {form: true};
    return null;
}
"""


class LoginError(RuntimeError):
    """统一身份认证无法在无头模式下完成。"""


def authenticate_page(page, username, password):
    """复用有效会话，或在确认的统一认证页面提交一次密码。"""
    page.goto(APP_URL, wait_until="domcontentloaded")
    state_args = {
        "appOrigin": APP_ORIGIN,
        "ssoOrigin": SSO_ORIGIN,
        "ssoPath": SSO_PATH,
        "submitted": False,
    }
    state = page.wait_for_function(LOGIN_STATE, arg=state_args).json_value()
    if state.get("form"):
        def check_origin():
            url = urlsplit(page.url)
            if (f"{url.scheme}://{url.netloc}", url.path) != (SSO_ORIGIN, SSO_PATH):
                raise LoginError("统一认证页面已跳转，请重新运行脚本。")

        if not page.locator("#un").is_visible():
            check_origin()
            page.locator("#password_login").click()
        check_origin()
        page.locator("#un").fill(username)
        check_origin()
        page.locator("#pd").fill(password)
        check_origin()
        page.locator("#index_login_btn").click()
        state_args["submitted"] = True
        state = page.wait_for_function(LOGIN_STATE, arg=state_args).json_value()

    if state.get("verification"):
        raise LoginError("学校要求验证码或二次验证，本次无头登录已停止。")
    if state.get("rejected"):
        raise LoginError("学校拒绝了登录，请检查账号、密码或账号状态。")
    return state["token"]


def save_auth_state(path, state):
    """以私有权限原子保存会话，避免中断时留下半个文件。"""
    path.parent.mkdir(mode=0o700, exist_ok=True)
    temp = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    temp_path = Path(temp.name)
    try:
        with temp:
            json.dump(state, temp)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


class SeatLogin:
    def __init__(self, session):
        try:
            from dotenv import dotenv_values
        except ImportError:
            raise LoginError("请先安装依赖：python -m pip install requests playwright python-dotenv") from None

        config = dotenv_values(PROJECT_DIR / ".env", interpolate=False)
        self.username = (os.environ.get("WHUT_USERNAME", config.get("WHUT_USERNAME")) or "").strip()
        self.password = os.environ.get("WHUT_PASSWORD", config.get("WHUT_PASSWORD"))
        if not self.username or not self.password:
            raise LoginError("请在项目目录的 .env 中设置 WHUT_USERNAME 和 WHUT_PASSWORD。")

        cache_key = hashlib.sha256(f"{session}\0{self.username}".encode()).hexdigest()
        self.cache_path = PROJECT_DIR / ".whut-auth" / f"{cache_key}.json"

    def authenticate(self, expired_token=None):
        with self.browser_page(expired_token) as (_, token):
            return token

    def renew_page(self, page, expired_token, *, on_page=None):
        """Renew authentication in the existing browser's fresh context."""
        context = page.context.browser.new_context(viewport={"width": 1440, "height": 900})
        try:
            replacement = context.new_page()
            replacement.set_default_timeout(20000)
            if on_page is not None:
                on_page(replacement)
            token = authenticate_page(replacement, self.username, self.password)
            if token == expired_token:
                raise LoginError("重新登录后仍收到已过期的 Token。")
            try:
                save_auth_state(self.cache_path, context.storage_state())
            except OSError:
                print(yellow("⚠️  已登录，会话缓存写入失败。"))
        except BaseException:
            context.close()
            raise
        page.context.close()
        return replacement, token

    @contextmanager
    def browser_page(self, expired_token=None, *, on_page=None):
        try:
            from playwright.sync_api import Error, TimeoutError, sync_playwright
        except ImportError:
            raise LoginError("请先安装 Playwright，再运行 python -m playwright install chromium。") from None

        state = None
        if expired_token is None:
            try:
                saved_state = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(saved_state, dict):
                    state = saved_state
            except (OSError, ValueError):
                pass

        print(cyan("🌐 正在通过无头浏览器登录座位预约系统..."))
        authenticated = False
        try:
            with sync_playwright() as driver:
                try:
                    browser = driver.chromium.launch(headless=True)
                except Error:
                    raise LoginError("Chromium 无法启动，请运行 python -m playwright install chromium 并检查系统依赖。") from None
                with browser:
                    try:
                        context = browser.new_context(storage_state=state, viewport={"width": 1440, "height": 900})
                    except Error:
                        if state is None:
                            raise
                        context = browser.new_context(viewport={"width": 1440, "height": 900})
                    page = context.new_page()
                    page.set_default_timeout(20000)
                    if on_page is not None:
                        on_page(page)
                    token = authenticate_page(page, self.username, self.password)
                    if token == expired_token:
                        raise LoginError("重新登录后仍收到已过期的 Token。")
                    try:
                        save_auth_state(self.cache_path, context.storage_state())
                    except (OSError, Error):
                        print(yellow("⚠️  已登录，会话缓存写入失败。"))
                    print(green("✅ 登录成功"))
                    authenticated = True
                    yield page, token
        except TimeoutError:
            if authenticated:
                raise
            raise LoginError("等待学校认证页面或预约 Token 超时，请检查校园网连接及学校认证状态。") from None
        except Error:
            if authenticated:
                raise
            raise LoginError("浏览器认证失败，请检查座位预约网站和校园网连接。") from None



# ============================================================
# API 调用
# ============================================================

class SeatRateLimitError(RuntimeError):
    """The seat server requested that the client stop sending requests."""


class RequestPacer:
    """Space repeated endpoints while allowing unrelated requests immediately."""
    def __init__(self, interval=3.5, *, clock=None, sleep=None):
        self.interval = interval
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.lock = Lock()
        self.endpoints = {}

    def wait(self, url):
        endpoint = urlsplit(url).path
        with self.lock:
            if endpoint not in self.endpoints:
                self.endpoints[endpoint] = (Lock(), None)
            endpoint_lock, _ = self.endpoints[endpoint]
        with endpoint_lock:
            with self.lock:
                _, previous = self.endpoints[endpoint]
            if previous is not None:
                delay = self.interval - (self.clock() - previous)
                if delay > 0:
                    self.sleep(delay)
            with self.lock:
                self.endpoints[endpoint] = (endpoint_lock, self.clock())


def prepare_reservation_page(page):
    """The CLI owns room queries; avoid the homepage's duplicate prefetch."""
    page.route(f"{API_BASE}/frontApi/res/findRoomDuration/**", lambda route: route.abort())


class SeatAPI:
    def __init__(self, login, token, *, captcha=None, pacer=None):
        self.login = login
        self.token = token
        self.refresh_lock = Lock()
        self.refresh_error = None
        self.sessions = []
        self.session_lock = Lock()
        self.thread = local()
        self.captcha = captcha
        self.pacer = pacer if pacer is not None else RequestPacer()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        for session in self.sessions:
            session.close()

    def _session(self):
        import requests

        if not hasattr(self.thread, "session"):
            self.thread.session = requests.Session()
            with self.session_lock:
                self.sessions.append(self.thread.session)
        return self.thread.session

    def _refresh_token(self, expired_token):
        with self.refresh_lock:
            if self.refresh_error is not None:
                raise LoginError(self.refresh_error)
            if self.token == expired_token:
                print(yellow("\n🔄 Token 过期，正在重新登录..."))
                try:
                    self.token = self.login(expired_token)
                except LoginError as exc:
                    self.refresh_error = str(exc)
                    raise

    def post(self, path, body=None, timeout=10, silent=False):
        """只在服务端明确拒绝过期 Token 时重新认证并重试一次。"""
        import requests

        url = f"{API_BASE}/{path}"
        session = self._session()
        for attempt in range(2):
            if attempt and self.captcha is not None and path.startswith("frontApi/make/freeBook/"):
                cap_token = self.captcha.token(self)
                url = f"{API_BASE}/{path.split('?', 1)[0]}?capToken={quote(cap_token, safe='')}"
            self.pacer.wait(url)
            token = self.token
            headers = {"Content-Type": "application/json", "token": token}
            try:
                response = session.post(url, json=body or {}, headers=headers, timeout=timeout)
                if response.status_code == 429:
                    endpoint = path.split('?', 1)[0]
                    raise SeatRateLimitError(f"图书馆接口限流（HTTP 429，{endpoint}），请稍后再试。")
                data = response.json()
            except (requests.RequestException, ValueError) as exc:
                if not silent:
                    print(yellow(f"\n⚠️  API 调用失败: {exc}"))
                return None
            if not isinstance(data, dict):
                return None
            message = data.get("message")
            if not data.get("status") and isinstance(message, str) and "过于频繁" in message:
                endpoint = path.split('?', 1)[0]
                raise SeatRateLimitError(f"图书馆接口限流（{endpoint}）：{message[:160]}")
            if data.get("status") or data.get("code") != 20003:
                return data
            if attempt:
                raise LoginError("重新登录后的 Token 仍被预约系统拒绝。")
            self._refresh_token(token)


# ============================================================
# 时间工具
# ============================================================

def to_minutes(time_str):
    """HH:MM → 分钟"""
    h, m = time_str.strip().split(":")
    return int(h) * 60 + int(m)


def to_str(minutes):
    """分钟 → HH:MM"""
    return f"{int(minutes) // 60:02d}:{int(minutes) % 60:02d}"


# ============================================================
# 核心流程
# ============================================================

def get_rooms(api, date_str, floor):
    """获取要抢的房间列表"""
    resp = api.post(
        f"frontApi/res/findRoomDuration/{BUILDING_ID}/{date_str}",
        body={"dateType": "0", "amPm": "0", "floorId": "0",
              "power": False, "window": False},
    )
    if not resp or not resp.get("status"):
        print(red("❌ 获取房间列表失败"))
        return []

    all_rooms = resp["data"]["pageList"]
    if not all_rooms:
        print(red("❌ 没有可用房间"))
        return []

    # 按楼层筛选
    if floor in FLOOR_NAMES:
        target = FLOOR_NAMES[floor]
        rooms = [r for r in all_rooms if r["floorName"] == target]
    else:
        rooms = all_rooms

    if not rooms:
        print(red(f"❌ 楼层 {floor} 没有可用房间"))
        return []

    print()
    for r in rooms:
        print(f"   📍 {bold(r['name'])} ({r['floorName']}) — "
              f"空闲 {green(str(r['seatFree']))} / 总数 {r['seatTotal']}")
    return rooms


def resolve_start_end(api, date_str, start_arg, duration_arg):
    """确定开始和结束分钟数。start_arg 是开始时间，duration_arg 是预约时长（分钟）。"""
    now = datetime.now()
    current_min = now.hour * 60 + now.minute

    # 开始时间：支持用户指定；不指定则从现在开始，对齐到 30 分钟
    if start_arg:
        start = to_minutes(start_arg)
    elif date_str > now.date().isoformat():
        start = 450
    else:
        start = max(current_min + 5, 450)   # 450 = 07:30
        start = ((start + 29) // 30) * 30   # 对齐到 30 分钟

    # 结束时间 — 通过 API 获取可用选项；负数时长表示预约到当天最晚可用结束时间
    resp = api.post(
        f"frontApi/res/getEndTimes2/{BUILDING_ID}/{start}",
    )

    if resp and resp.get("status") and resp.get("data"):
        options = resp["data"]  # [[minute_str, "HH:MM"], ...]
        max_end = int(options[-1][0])  # 最晚可用结束时间

        if duration_arg and duration_arg > 0:
            # 用户指定了时长（分钟）
            target = start + duration_arg
            end = None
            for opt in options:
                if int(opt[0]) >= target:
                    end = int(opt[0])
                    break
            if end is None:
                end = max_end  # 超出范围就用最晚的
        else:
            end = max_end  # 默认最长；负数时长也预约到当天最晚可用结束时间
    else:
        # fallback
        print(yellow("   ⚠️  无法获取精确结束时间，用默认值"))
        end = 1320 if duration_arg and duration_arg < 0 else min(start + (duration_arg or 840), 1320)

    return start, end

def get_seat_timeline(api, seat_id, date_str):
    """获取某个座位在指定日期的实际空闲时间段，失败返回 None"""
    resp = api.post(
        f"frontApi/res/getTimeLine/{seat_id}/{date_str}",
        timeout=5,
        silent=True,
    )
    if resp and resp.get("status") and resp.get("data"):
        return resp["data"]  # {markList, freeList}
    return None


def parse_timeline_range(label):
    """解析 '09:19-10:30' → (559, 630)"""
    start_str, end_str = label.split("-")
    return to_minutes(start_str), to_minutes(end_str)


def find_candidate(api, room, date_str, target_start, target_end, min_duration=0):
    """在指定房间找符合条件的空闲座位，但不提交预约。

    min_duration > 0 时，只返回时长 >= min_duration 分钟的座位；
    否则返回当前可用时间最长的座位。

    直接查每个 FREE 座位的 getTimeLine 精确时间线，不依赖 freeSeatIdsSlice 的粗粒度时段。
    getTimeLine 超时的座位自动跳过。
    """
    room_id = room["id"]
    room_name = room["name"]

    seats = api.post(
        f"frontApi/res/freeSeatIdsSlice/{room_id}/{date_str}",
        body={"beginMinute": target_start, "endMinute": target_end,
              "minMinute": 0, "amPm": 0},
    )
    if not seats or not seats.get("status"):
        return False, "API 错误"

    BLACKLIST = {"001", "002", "003", "004", "005", "187", "188", "189", "190", "191", "192", "193", "194","85"}

    free_candidates = [
        (sid, info) for sid, info in seats["data"].items()
        if info.get("status") == "FREE" and info.get("label") not in BLACKLIST
    ]
    if not free_candidates:
        return None, None

    best_seat, best_s, best_e, best_dur = None, 0, 0, 0
    for sid, info in free_candidates:
        tl = get_seat_timeline(api, info["id"], date_str)
        if not tl:
            continue
        for slot in tl.get("freeList", []):
            sl, el = parse_timeline_range(slot["label"])
            a = max(sl, target_start)
            b = min(el, target_end)
            if a >= b:
                continue
            sdur = b - a
            if min_duration > 0:
                if sdur >= min_duration and sdur > best_dur:
                    best_seat, best_s, best_e, best_dur = info, a, b, sdur
            else:
                if sdur > best_dur:
                    best_seat, best_s, best_e, best_dur = info, a, b, sdur
        if best_dur == target_end - target_start:
            break

    if best_seat is None:
        if min_duration > 0:
            return None, f"有 {len(free_candidates)} 个空位但都不满足至少 {min_duration} 分钟的要求"
        return None, None

    return True, {
        "seat_id": best_seat["id"],
        "seat_label": best_seat.get("label", "?"),
        "seat_name": best_seat.get("name", "?"),
        "room_name": room_name,
        "start": best_s,
        "end": best_e,
    }


def book_candidate(api, candidate, date_str):
    """提交预约。"""
    cap_token = api.captcha.token(api) if api.captcha is not None else "capToken"
    resp = api.post(
        f"frontApi/make/freeBook/{candidate['seat_id']}/{date_str}/{candidate['start']}/{candidate['end']}?capToken={quote(cap_token, safe='')}",
    )
    if resp and resp.get("status"):
        data = resp.get("data", {})
        candidate = dict(candidate)
        candidate["message"] = data.get("message", "")
        return True, candidate
    err_msg = resp.get("message", "未知错误") if resp else "网络错误"
    return False, err_msg


# ============================================================
# 抢座主循环
# ============================================================

def run(session, args, *, confirm=None):
    login = SeatLogin(session)
    with login.browser_page(on_page=prepare_reservation_page) as (page, token):
        captcha = ReservationCaptcha(page, API_BASE)

        def refresh(expired_token):
            captcha.page, token = login.renew_page(captcha.page, expired_token, on_page=prepare_reservation_page)
            return token

        with SeatAPI(refresh, token, captcha=captcha) as api:
            return run_reservation(api, args, confirm)


def run_reservation(api, args, confirm):
    date_str = args.date or date.today().isoformat()

    rooms = get_rooms(api, date_str, args.floor)
    if not rooms:
        return

    start, end = resolve_start_end(api, date_str, args.start, args.duration)
    if start is None:
        return

    dur = end - start
    if args.duration and args.duration < 0:
        required_duration = dur
        duration_note = f"必须满足至少 {required_duration} 分钟（直到22:00）"
    elif args.duration and args.duration > 0:
        required_duration = args.duration
        duration_note = f"必须满足至少 {required_duration} 分钟"
    else:
        required_duration = 0
        duration_note = "自动选择最长可用时段"
    print(f"⏰ 目标: {to_str(start)} ~ {to_str(end)}  ({dur}分钟) — {duration_note}")

    if args.max_attempts == 0:
        print(f"\n{cyan('🔄 开始抢座... (无限循环，Ctrl+C 停止)')}")
    else:
        print(f"\n{cyan('🔄 开始抢座...')}")
    print("-" * 50)

    i = 0
    while args.max_attempts == 0 or i < args.max_attempts:
        i += 1
        round_started = time.monotonic()
        verification_retry_after = None
        room = random.choice(rooms)  # 多房间时随机选
        success, result = find_candidate(api, room, date_str, start, end, min_duration=required_duration)

        if success:
            candidate = result
            if not args.auto_confirm:
                if confirm is None:
                    print(f"\n{green('✅ 找到符合条件的座位')}")
                    print(f"   📅 日期: {date_str}")
                    print(f"   ⏰ 时间: {to_str(candidate['start'])} ~ {to_str(candidate['end'])}  ({candidate['end'] - candidate['start']}分钟)")
                    print(f"   🪑  座位: {bold(candidate['seat_label'])}  ({candidate['seat_name']})")
                    print(f"   📍 位置: {candidate['room_name']}")
                    accepted = input("确认预约这个座位？(y/N) ").strip().lower() == "y"
                else:
                    accepted = confirm(candidate, date_str)
                if not accepted:
                    delay = args.interval + random.uniform(-1, 1)
                    print(yellow(f"已跳过，继续刷新 | 等 {delay:.1f}s"))
                    time.sleep(max(0.5, delay))
                    continue

            try:
                success, result = book_candidate(api, candidate, date_str)
            except CaptchaAttemptError as exc:
                success, result = False, str(exc)
                verification_retry_after = exc.retry_after

        if success:
            print(f"\n{green('=' * 50)}")
            print(green("🎉 抢座成功！"))
            print(f"{'=' * 50}")
            print(f"   📅 日期: {date_str}")
            print(f"   ⏰ 时间: {to_str(result['start'])} ~ {to_str(result['end'])}  ({result['end'] - result['start']}分钟)")
            print(f"   🪑  座位: {bold(result['seat_label'])}  ({result['seat_name']})")
            print(f"   📍 位置: {result['room_name']}")
            if result["message"]:
                print(f"   💬 {result['message']}")
            print(f"{'=' * 50}\n")
            return True

        if result is None:
            # 无空闲
            delay = args.interval + random.uniform(-1, 1)
            print(f"[{i}] {red('🈵')} {room['name']} 无空闲 | 等 {delay:.1f}s",
                  end="\r" if i % 20 != 0 else "\n", flush=True)
            time.sleep(max(0.5, delay))
        elif isinstance(result, str):
            # 预订失败，返回错误消息字符串
            msg = result
            print(f"[{i}] {red('✗')} {msg}")
            # 终端错误：已有预约 / 黑名单 → 停止
            if any(kw in msg for kw in ["已有预约", "存在未结束的预约", "黑名单", "不能预约"]):
                print(yellow(f"\n⚠️  {msg}，停止。"))
                return
            if verification_retry_after is not None and args.max_attempts and i >= args.max_attempts:
                continue
            delay = args.interval + random.uniform(-1, 1)
            if verification_retry_after is not None:
                delay = max(0, delay - (time.monotonic() - round_started), verification_retry_after)
                print(yellow(f"[{i}] 等待 {max(0.5, delay):.1f} 秒后继续查找并验证。"))
            time.sleep(max(0.5, delay))

    if args.max_attempts > 0:
        print(red(f"\n❌ 已达最大尝试次数 ({args.max_attempts})"))


# ============================================================
# CLI
# ============================================================

def interactive():
    """交互式引导：一步步询问参数，直接回车选默认值"""
    from datetime import timedelta

    print(bold("\n🎓 武汉理工大学图书馆 · 座位预约抢座"))
    print("━" * 44)

    D = "[直接回车]"

    # 楼层
    print(f"\n请选择楼层 {D}：")
    print("  1) 六楼  |  2) 七楼  |  3) 全部（默认）")
    ans = input("> ").strip()
    if ans not in ("1", "2"):
        ans = "3"
    floor = {"1": 6, "2": 7, "3": 0}[ans]

    # 日期
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    print(f"\n预约日期 {D}：")
    print(f"  1) 今天 ({today})（默认）")
    print(f"  2) 明天 ({tomorrow})")
    print(f"  3) 自定义")
    ans = input("> ").strip()
    if ans == "2":
        date_str = tomorrow
    elif ans == "3":
        date_str = input("  请输入日期 (YYYY-MM-DD): ").strip()
    else:
        date_str = today  # 默认今天

    # 开始时间
    print(f"\n从何时开始预约 {D}：")
    print(f"  1) 现在开始（默认）")
    print(f"  2) 自定义开始时间")
    ans = input("> ").strip()
    if ans == "2":
        start_time = input("  请输入开始时间 (HH:MM): ").strip()
    else:
        start_time = None

    # 时长
    print(f"\n预约时长 {D}：")
    print(f"  1) 自动最长（默认）|  2) 2h  |  3) 3h  |  4) 4h  |  5) 自定义")
    ans = input("> ").strip()
    dur_map = {"1": 0, "2": 120, "3": 180, "4": 240}
    if ans == "5":
        duration = int(input("  请输入时长（分钟，负数=直到22:00）: ").strip())
    elif ans in dur_map:
        duration = dur_map[ans]
    else:
        # 直接输入数字（包括负数），不在 1-5 范围内的都当作自定义时长
        try:
            duration = int(ans)
        except (ValueError, TypeError):
            duration = 0  # 解析失败，回退到自动最长

    # 刷新间隔
    print(f"\n刷新间隔({D} 3秒)：")
    interval = input("> ").strip()
    interval = float(interval) if interval else 3.0

    # 最大次数
    print(f"\n最大尝试次数({D} 无限)：")
    print(f"  直接回车=无限，输入数字=限制次数")
    max_attempts = input("> ").strip()
    max_attempts = int(max_attempts) if max_attempts else 0

    # 自动确认
    print(f"\n找到符合座位后自动确认预约？({D} Y)：")
    print("  Y=自动预约（默认） | n=每次找到后手动确认")
    ans = input("> ").strip().lower()
    auto_confirm = not (ans == "n")

    # 汇总确认
    floor_txt = f"{floor}楼" if floor in FLOOR_NAMES else "全部"
    if duration < 0:
        dur_txt = "直到22:00"
    elif duration:
        dur_txt = f"{duration}分钟"
    else:
        dur_txt = "自动（最长）"
    att_txt = "无限" if max_attempts == 0 else str(max_attempts)

    start_txt = start_time or "现在开始"

    print()
    print(bold("━" * 44))
    print(bold("  确认配置："))
    print(f"    楼层: {floor_txt}")
    print(f"    日期: {date_str}")
    print(f"    开始: {start_txt}")
    print(f"    时长: {dur_txt}")
    print(f"    刷新: {interval}s  |  最多 {att_txt} 次")
    print(f"    确认: {'自动预约' if auto_confirm else '手动确认'}")
    print(bold("━" * 44))
    print()
    ans = input("开始抢座？(Y/n) ").strip().lower()
    if ans and ans != "y":
        print("已取消。")
        return

    # 构造 args
    import argparse
    ns = argparse.Namespace(
        floor=floor, date=date_str, start=start_time, duration=duration,
        interval=interval, max_attempts=max_attempts,
        auto_confirm=auto_confirm,
        once=False, session="whut",
    )
    run(ns.session, ns)


def main():
    # 如果没有任何参数传入，进入交互模式
    if len(sys.argv) == 1:
        interactive()
        return

    p = argparse.ArgumentParser(
        description="武汉理工大学图书馆 · 座位预约抢座脚本",
        epilog="通过 Playwright 无头浏览器登录。"
    )
    p.add_argument("-f", "--floor", type=int, default=0,
                   help="楼层：6=六楼, 7=七楼, 其他=全部 (默认: 0)")
    p.add_argument("-d", "--date", help="日期 YYYY-MM-DD (默认: 今天)")
    p.add_argument("-s", "--start", help="开始时间 HH:MM (默认: 现在开始)")
    p.add_argument("-t", "--duration", type=int, default=0,
                   help="预约时长（分钟），不填则自动选最长时间段")
    p.add_argument("--interval", type=float, default=3.0,
                   help="刷新间隔秒 (默认: 3)")
    p.add_argument("--max-attempts", type=int, default=0,
                   help="最大尝试次数 (0=无限, 默认: 0)")
    p.add_argument("--once", action="store_true", help="只试一次")
    p.add_argument("--no-auto-confirm", dest="auto_confirm", action="store_false",
                   help="找到符合条件的座位后不自动预约，改为手动确认")
    p.set_defaults(auto_confirm=True)
    p.add_argument("--session", default="whut", help="登录会话缓存名称 (默认: whut)")
    p.add_argument("--login-only", action="store_true", help="只检查登录，登录成功后退出")
    args = p.parse_args()

    if args.login_only:
        SeatLogin(args.session).authenticate()
        return

    if args.once:
        args.max_attempts = 1

    floor_txt = f"{args.floor}楼" if args.floor in FLOOR_NAMES else "全部"
    att_txt = "无限" if args.max_attempts == 0 else str(args.max_attempts)

    print(bold("\n🏛️  武汉理工大学图书馆 · 座位预约抢座"))
    print("━" * 44)
    print(f"   楼层: {floor_txt}  |  日期: {args.date or '今天'}")
    print(f"   开始: {args.start or '现在开始'}")
    if args.duration < 0:
        time_str = "直到22:00"
    elif args.duration:
        time_str = f"{args.duration}分钟"
    else:
        time_str = "自动（最长）"
    print(f"   时长: {time_str}")
    print(f"   确认: {'自动预约' if args.auto_confirm else '手动确认'}")
    print(f"   刷新: {args.interval}s  |  最多 {att_txt} 次")
    print("━" * 44)

    run(args.session, args)


if __name__ == "__main__":
    try:
        main()
    except (LoginError, SeatRateLimitError, CaptchaError) as exc:
        print(red(f"❌ {exc}"), file=sys.stderr)
        sys.exit(1)
