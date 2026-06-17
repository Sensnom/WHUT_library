#!/usr/bin/env python3
"""
武汉理工大学南湖图书馆 — 座位预约抢座脚本
跨平台：Linux / Windows / macOS
依赖：pip install requests (Python 3.8+)
需要：opencli (npm i -g @jackwener/opencli)

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
import json
import subprocess
import sys
import time
import random
from datetime import datetime, date

# ============================================================
# 配置常量
# ============================================================

API_BASE = "https://zw.whut.edu.cn/jsq/static"
BUILDING_ID = "2009170845461745664"  # 南湖馆
APP_URL = "https://zw.whut.edu.cn/jsq-v/"

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
# opencli 桥接
# ============================================================

def opencli(session, args_list, timeout=15):
    """调用 opencli browser <session> ..."""
    cmd = ["opencli", "browser", session] + args_list
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.stdout.strip(), p.stderr.strip(), p.returncode


def eval_js(session, js_code, timeout=10):
    """在页面中执行 JS 返回纯文本"""
    out, _, rc = opencli(session, ["eval", js_code], timeout=timeout)
    return out if rc == 0 else None


def get_token(session):
    """从浏览器 sessionStorage 获取 auth token"""
    t = eval_js(session, "sessionStorage.getItem('jsq_p-token')")
    return t if t and len(t) > 5 else None


def get_user_info(session):
    """获取登录用户信息"""
    raw = eval_js(session, "sessionStorage.getItem('jsq_p-userInfo')")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return None


# ============================================================
# API 调用
# ============================================================

_SESSION = "whut"  # 模块级，run() 里会更新


def set_session(name):
    global _SESSION
    _SESSION = name


def api_post(path, token, body=None, timeout=10, silent=False):
    """调用座位系统 API。silent=True 时超时不打印警告。"""
    import requests
    url = f"{API_BASE}/{path}"
    headers = {"Content-Type": "application/json", "token": token}
    try:
        r = requests.post(url, json=body or {}, headers=headers, timeout=timeout)
        data = r.json()
        if not data.get("status") and data.get("code") == 20003:
            print(yellow("\n🔄 Token 过期，刷新页面..."))
            new_token = refresh_token(_SESSION)
            if new_token:
                print(green("✅ Token 已刷新"))
            return {"status": True, "code": -1, "message": "token-expired", "data": {}}
        return data
    except Exception as e:
        if not silent:
            print(yellow(f"\n⚠️  API 调用失败: {e}"))
        return None


def refresh_token(session):
    """Token 过期后刷新页面并重新获取 token。"""
    eval_js(session, "window.location.reload()")
    time.sleep(5)
    new_token = get_token(session)
    if new_token:
        return new_token
    return None


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

def wait_for_login(session):
    """确保有已登录的页面，返回 (token, user)。如果已有则直接复用。"""

    # 先检查当前 session 是否已登录
    token = get_token(session)
    if token:
        user = get_user_info(session)
        name = user.get("fullName", "?") if user else "?"
        print(f"\n{green('✅ 复用已有登录态')} — {bold(name)}")
        return token, user

    print(f"\n{cyan('🌐 打开座位预约系统...')}")
    print(f"{yellow('📌 请在弹出的浏览器窗口完成统一身份认证登录！')}\n")

    # 打开主页
    opencli(session, ["open", APP_URL], timeout=20)

    print(f"{bold('⏳ 等待登录...')}", end="", flush=True)
    for i in range(1, 999):
        time.sleep(2)
        token = get_token(session)
        if token:
            user = get_user_info(session)
            name = user.get("fullName", "?") if user else "?"
            print(f"\n{green('✅ 登录成功！')} 欢迎 {bold(name)}")
            return token, user
        if i % 10 == 0:
            print(f"\n{yellow('⏳ 仍在等待登录...')}")
        print(".", end="", flush=True)

    print(red("\n❌ 超时"))
    sys.exit(1)


def get_rooms(token, date_str, floor):
    """获取要抢的房间列表"""
    resp = api_post(
        f"frontApi/res/findRoomDuration/{BUILDING_ID}/{date_str}",
        token,
        body={"dateType": "0", "amPm": "0", "floorId": "0",
              "power": False, "window": False},
    )
    if not resp or (not resp.get("status") and resp.get("code") != -1):
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


def resolve_start_end(token, date_str, start_arg, duration_arg):
    """确定开始和结束分钟数。start_arg 是开始时间，duration_arg 是预约时长（分钟）。"""
    now = datetime.now()
    current_min = now.hour * 60 + now.minute

    # 开始时间：支持用户指定；不指定则从现在开始，对齐到 30 分钟
    if start_arg:
        start = to_minutes(start_arg)
    else:
        start = max(current_min + 5, 450)   # 450 = 07:30
        start = ((start + 29) // 30) * 30   # 对齐到 30 分钟

    # 结束时间 — 通过 API 获取可用选项
    # 负数时长 = 预约到 22:00，直接返回，无需调 API
    if duration_arg and duration_arg < 0:
        return start, 1320

    resp = api_post(
        f"frontApi/res/getEndTimes2/{BUILDING_ID}/{start}",
        token,
    )

    if resp and resp.get("status") and resp.get("data"):
        options = resp["data"]  # [[minute_str, "HH:MM"], ...]
        max_end = int(options[-1][0])  # 最晚可用结束时间

        if duration_arg:
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
            end = max_end  # 默认最长
    else:
        # fallback
        print(yellow("   ⚠️  无法获取精确结束时间，用默认值"))
        end = min(start + (duration_arg or 840), 1320)

    return start, end

def get_seat_timeline(token, seat_id, date_str):
    """获取某个座位在指定日期的实际空闲时间段，失败返回 None"""
    try:
        resp = api_post(
            f"frontApi/res/getTimeLine/{seat_id}/{date_str}",
            token,
            timeout=5,
            silent=True,
        )
        if resp and resp.get("status") and resp["data"]:
            return resp["data"]  # {markList, freeList}
    except Exception:
        pass
    return None


def parse_timeline_range(label):
    """解析 '09:19-10:30' → (559, 630)"""
    start_str, end_str = label.split("-")
    return to_minutes(start_str), to_minutes(end_str)


def find_candidate(token, room, date_str, target_start, target_end, min_duration=0):
    """在指定房间找符合条件的空闲座位，但不提交预约。

    min_duration > 0 时，只返回时长 >= min_duration 分钟的座位；
    否则返回当前可用时间最长的座位。

    直接查每个 FREE 座位的 getTimeLine 精确时间线，不依赖 freeSeatIdsSlice 的粗粒度时段。
    getTimeLine 超时的座位自动跳过。
    """
    room_id = room["id"]
    room_name = room["name"]

    seats = api_post(
        f"frontApi/res/freeSeatIdsSlice/{room_id}/{date_str}",
        token,
        body={"beginMinute": target_start, "endMinute": target_end,
              "minMinute": 0, "amPm": 0},
    )
    if not seats or not seats.get("status"):
        return False, "API 错误"

    BLACKLIST = {"001", "002", "003", "004", "005", "187", "188", "189", "190", "191", "192", "193", "194"}

    free_candidates = [
        (sid, info) for sid, info in seats["data"].items()
        if info.get("status") == "FREE" and info.get("label") not in BLACKLIST
    ]
    if not free_candidates:
        return None, None

    best_seat, best_s, best_e, best_dur = None, 0, 0, 0
    for sid, info in free_candidates:
        tl = get_seat_timeline(token, info["id"], date_str)
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


def book_candidate(token, candidate, date_str):
    """提交预约。"""
    resp = api_post(
        f"frontApi/make/freeBook/{candidate['seat_id']}/{date_str}/{candidate['start']}/{candidate['end']}?capToken=",
        token,
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

def run(session, args):
    set_session(session)
    token, user = wait_for_login(session)

    date_str = args.date or date.today().isoformat()

    rooms = get_rooms(token, date_str, args.floor)
    if not rooms:
        return

    start, end = resolve_start_end(token, date_str, args.start, args.duration)
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
        room = random.choice(rooms)  # 多房间时随机选
        success, result = find_candidate(token, room, date_str, start, end, min_duration=required_duration)

        if success:
            candidate = result
            if not args.auto_confirm:
                print(f"\n{green('✅ 找到符合条件的座位')}")
                print(f"   📅 日期: {date_str}")
                print(f"   ⏰ 时间: {to_str(candidate['start'])} ~ {to_str(candidate['end'])}  ({candidate['end'] - candidate['start']}分钟)")
                print(f"   🪑  座位: {bold(candidate['seat_label'])}  ({candidate['seat_name']})")
                print(f"   📍 位置: {candidate['room_name']}")
                ans = input("确认预约这个座位？(y/N) ").strip().lower()
                if ans != "y":
                    delay = args.interval + random.uniform(-1, 1)
                    print(yellow(f"已跳过，继续刷新 | 等 {delay:.1f}s"))
                    time.sleep(max(0.5, delay))
                    continue

            success, result = book_candidate(token, candidate, date_str)

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
            return

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
            if any(kw in msg for kw in ["已有预约", "黑名单", "不能预约"]):
                print(yellow(f"\n⚠️  {msg}，停止。"))
                return
            delay = args.interval + random.uniform(-1, 1)
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
        epilog="跨平台 Python 脚本。需要 opencli + requests。"
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
    p.add_argument("--session", default="whut", help="opencli session 名")
    args = p.parse_args()

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
    main()
