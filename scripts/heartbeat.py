#!/usr/bin/env python3
"""
A-SOUL 直播心跳挂机 — 检测成员开播 → 移动端心跳涨亲密度。
使用 X25Kn E/X 协议（纯 Python HMAC 签名，无需外部服务）。
严格遵循服务端下发的心跳间隔，避免 time check failed。
需要成员正在直播才有效。
"""

import argparse
import hashlib
import json
import os
import random
import string
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import uuid
from pathlib import Path
from typing import Optional, Dict, List

from check_auth import check_login

_DISCORD_TARGET = "user:1479415368249507881"


def _notify(msg: str):
    """发送 Discord 通知"""
    try:
        subprocess.run(
            ["openclaw", "message", "send",
             "--channel", "discord",
             "--target", _DISCORD_TARGET,
             "--message", msg],
            timeout=15, check=False
        )
    except Exception:
        pass


_LOCK_DIR = Path("/tmp/asoul_heartbeat_locks")
_LOG_FILE = Path.home() / ".openclaw" / "logs" / "asoul_activity.jsonl"


def _log(event: dict):
    """追加一条活动记录到日志文件"""
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({**event, "ts": int(time.time())}, ensure_ascii=False) + "\n")

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
_DANMAKU_MSGS = [
    "来了来了", "❤️❤️❤️", "加油", "冲冲冲", "一直在",
    "支持", "好好好", "比心", "干杯", "爱了爱了",
    "哈哈哈哈", "早上好", "晚上好", "冲鸭",
]

MEMBERS = [
    {"name": "嘉然",   "uid": 672328094,         "room": 22637261},
    {"name": "贝拉",   "uid": 672353429,         "room": 22632424},
    {"name": "乃琳",   "uid": 672342685,         "room": 22625027},
    {"name": "心宜",   "uid": 3537115310721181,  "room": 30849777},
    {"name": "思诺",   "uid": 3537115310721781,  "room": 30858592},
]

WATCH_MINUTES = 25
HEARTBEAT_INTERVAL = 60

_COOKIE_PATHS = [
    Path(__file__).resolve().parent.parent / ".cookies.json",
    Path(__file__).resolve().parent.parent.parent / "bilibili-live-checkin" / ".cookies.json",
]


def load_cookies() -> Optional[Dict[str, str]]:
    for p in _COOKIE_PATHS:
        if p.exists():
            try:
                with open(p, "r") as f:
                    data = json.load(f)
                if data.get("SESSDATA") and data.get("bili_jct"):
                    return data
            except Exception:
                continue
    return None


def _make_headers(sessdata: str, bili_jct: str, referer: str = "https://live.bilibili.com") -> dict:
    return {
        "User-Agent": _UA,
        "Cookie": f"SESSDATA={sessdata}; bili_jct={bili_jct}",
        "Origin": "https://live.bilibili.com",
        "Referer": referer,
    }


def _get_json(url: str, headers: dict, timeout: int = 10) -> Optional[dict]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _post_form(url: str, data: dict, headers: dict, timeout: int = 10) -> Optional[dict]:
    form = urllib.parse.urlencode(data).encode("utf-8")
    headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=form, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"code": -1, "message": str(e)}


def _post_json(url: str, data: dict, timeout: int = 10) -> Optional[dict]:
    """发送 JSON POST 请求"""
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _send_danmaku(room_id: int, msg: str, sessdata: str, bili_jct: str) -> bool:
    """发送一条弹幕"""
    url = "https://api.live.bilibili.com/msg/send"
    data = {
        "bubble": "0",
        "msg": msg,
        "color": "16777215",
        "mode": "1",
        "fontsize": "25",
        "rnd": str(int(time.time())),
        "roomid": str(room_id),
        "csrf": bili_jct,
        "csrf_token": bili_jct,
    }
    headers = _make_headers(sessdata, bili_jct, f"https://live.bilibili.com/{room_id}")
    resp = _post_form(url, data, headers)
    return resp is not None and resp.get("code") == 0


def light_up_medal(room_id: int, sessdata: str, bili_jct: str, count: int = 10):
    """开播时发10条弹幕点亮粉丝牌"""
    import random
    msgs = _DANMAKU_MSGS[:]
    random.shuffle(msgs)
    sent = 0
    for msg in msgs[:count]:
        if _send_danmaku(room_id, msg, sessdata, bili_jct):
            sent += 1
        time.sleep(3)
    print(f"    💬 弹幕点亮：{sent}/{count} 条", file=sys.stderr)
    return sent


# ──────────────────────────────────────────
# Live Status Detection
# ──────────────────────────────────────────

def check_live_status(room_ids: List[int], sessdata: str, bili_jct: str) -> Dict[int, Dict]:
    """批量查询直播间状态，返回 {room_id: {live_status, title, ...}}"""
    url = f"https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
    headers = _make_headers(sessdata, bili_jct)

    form_parts = []
    for m in MEMBERS:
        if m["room"] in room_ids:
            form_parts.append(f"uids[]={m['uid']}")
    form_body = "&".join(form_parts).encode("utf-8")

    headers_with_ct = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=form_body, headers=headers_with_ct)

    result = {}
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("code") == 0 and body.get("data"):
            uid_to_room = {m["uid"]: m["room"] for m in MEMBERS}
            for uid_str, info in body["data"].items():
                uid = int(uid_str)
                if uid in uid_to_room:
                    result[uid_to_room[uid]] = {
                        "live_status": info.get("live_status", 0),
                        "title": info.get("title", ""),
                        "area_name": info.get("area_v2_name", ""),
                    }
    except Exception:
        pass

    for room_id in room_ids:
        if room_id not in result:
            info = _get_room_info(room_id, sessdata, bili_jct)
            if info:
                result[room_id] = info

    return result


def _get_room_info(room_id: int, sessdata: str, bili_jct: str) -> Optional[Dict]:
    """获取直播间详细信息，包含 area_id / parent_area_id"""
    url = f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}"
    headers = _make_headers(sessdata, bili_jct)
    resp = _get_json(url, headers)
    if resp and resp.get("code") == 0:
        data = resp["data"]
        return {
            "live_status": data.get("live_status", 0),
            "title": data.get("title", ""),
            "area_name": data.get("area_name", ""),
            "area_id": data.get("area_id", 0),
            "parent_area_id": data.get("parent_area_id", 0),
        }
    return None


def _get_single_room_status(room_id: int, sessdata: str, bili_jct: str) -> Optional[Dict]:
    """简化版：只返回 live_status"""
    return _get_room_info(room_id, sessdata, bili_jct)


# ──────────────────────────────────────────
# Room Entry
# ──────────────────────────────────────────

def enter_room(room_id: int, sessdata: str, bili_jct: str) -> bool:
    url = "https://api.live.bilibili.com/xlive/web-room/v1/index/roomEntryAction"
    headers = _make_headers(sessdata, bili_jct, f"https://live.bilibili.com/{room_id}")
    data = {
        "room_id": str(room_id),
        "platform": "pc",
        "csrf": bili_jct,
        "csrf_token": bili_jct,
    }
    resp = _post_form(url, data, headers)
    return resp is not None and resp.get("code") == 0


# ──────────────────────────────────────────
# X25Kn Heartbeat (涨亲密度，2026 现行 Web 协议)
# 协议：先 E（Enter）拿到 secret_key/secret_rule，之后每 60s 一次 X，
# 每个 X 用上一次响应里的 secret_key 做 HMAC 链式签名。
# 参考：RayWangQvQ/BiliBiliToolPro LiveDomainService.cs
# ──────────────────────────────────────────

_X25KN_E_URL = "https://live-trace.bilibili.com/xlive/data-interface/v1/x25Kn/E"
_X25KN_X_URL = "https://live-trace.bilibili.com/xlive/data-interface/v1/x25Kn/X"
_X25KN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# HMAC 哈希函数表（rule index → hashlib name）
_X25KN_HMAC_FUNCS = ["md5", "sha1", "sha256", "sha224", "sha512", "sha384"]


def _random_string(length: int) -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _wait_for_heartbeat_window(last_response_at: float, interval: int) -> float:
    """Wait only for the unspent part of the server heartbeat interval."""
    wait_seconds = interval - (time.monotonic() - last_response_at)
    if wait_seconds > 0:
        time.sleep(wait_seconds)
        return wait_seconds
    return 0.0


def _x25kn_sign(payload_json: str, rules: List[int], secret_key: str) -> str:
    """按 secret_rule 用 secret_key 做 HMAC 链式签名"""
    import hmac
    result = payload_json
    key_bytes = secret_key.encode("utf-8")
    for r in rules:
        if 0 <= r < len(_X25KN_HMAC_FUNCS):
            mac = hmac.new(key_bytes, result.encode("utf-8"),
                           getattr(hashlib, _X25KN_HMAC_FUNCS[r]))
            result = mac.hexdigest()
    return result


def _fetch_live_buvid(sessdata: str, bili_jct: str) -> Optional[str]:
    """走 SPI 接口拿 B 站签发的 buvid（b_3），可直接当 LIVE_BUVID 用"""
    url = "https://api.bilibili.com/x/frontend/finger/spi"
    req = urllib.request.Request(url, headers={
        "User-Agent": _X25KN_UA,
        "Cookie": f"SESSDATA={sessdata}; bili_jct={bili_jct}",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("code") == 0 and body.get("data", {}).get("b_3"):
            return body["data"]["b_3"]
    except Exception as e:
        print(f"    ⚠️  获取 LIVE_BUVID 失败: {e}", file=sys.stderr)
    return None


def _ensure_live_buvid(sessdata: str, bili_jct: str, cookies_path: Optional[Path] = None) -> Optional[str]:
    """从 cookies.json 读 LIVE_BUVID；没有就抓一次并写回"""
    cookies_path = cookies_path or _COOKIE_PATHS[0]
    if cookies_path.exists():
        try:
            data = json.load(open(cookies_path))
            if data.get("LIVE_BUVID"):
                return data["LIVE_BUVID"]
        except Exception:
            data = {}
    else:
        data = {"SESSDATA": sessdata, "bili_jct": bili_jct}

    buvid = _fetch_live_buvid(sessdata, bili_jct)
    if not buvid:
        return None
    try:
        data["LIVE_BUVID"] = buvid
        with open(cookies_path, "w") as f:
            json.dump(data, f)
        os.chmod(cookies_path, 0o600)
        print(f"    💾 已缓存 LIVE_BUVID 到 {cookies_path}", file=sys.stderr)
    except Exception:
        pass
    return buvid


def _x25kn_post(url: str, form: dict, sessdata: str, bili_jct: str,
                referer: str) -> Optional[Dict]:
    body = urllib.parse.urlencode(form).encode("utf-8")
    headers = {
        "User-Agent": _X25KN_UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": f"SESSDATA={sessdata}; bili_jct={bili_jct}",
        "Origin": "https://live.bilibili.com",
        "Referer": referer,
    }
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"code": -1, "message": str(e)}


def x25kn_enter_room(room_id: int, parent_id: int, area_id: int, up_id: int,
                     buvid: str, uuid_str: str,
                     sessdata: str, bili_jct: str) -> Optional[Dict]:
    """E：进入房间，返回 {timestamp, secret_key, secret_rule, heartbeat_interval} 或 None"""
    ts = _now_ms()
    form = {
        "id": json.dumps([parent_id, area_id, 0, room_id], separators=(",", ":")),
        "device": json.dumps([buvid, uuid_str], separators=(",", ":")),
        "ts": ts,
        "is_patch": 0,
        "heart_beat": "[]",
        "ua": _X25KN_UA,
        "csrf_token": bili_jct,
        "csrf": bili_jct,
        "visit_id": "",
        "ruid": up_id,
    }
    resp = _x25kn_post(_X25KN_E_URL, form, sessdata, bili_jct,
                       f"https://live.bilibili.com/{room_id}")
    if resp and resp.get("code") == 0 and resp.get("data"):
        return resp["data"]
    code = (resp or {}).get("code", "?")
    msg = (resp or {}).get("message", str(resp))
    print(f"    ⚠️  E 心跳失败: [{code}] {msg}", file=sys.stderr)
    _log({"type": "x25kn_e_error", "room_id": room_id, "code": code, "message": msg})
    return None


def x25kn_heartbeat(room_id: int, parent_id: int, area_id: int, up_id: int,
                    seq: int,
                    buvid: str, uuid_str: str,
                    ets: int, secret_key: str, secret_rule: List[int],
                    heartbeat_interval: int,
                    sessdata: str, bili_jct: str) -> Optional[Dict]:
    """X：心跳，返回 {timestamp, secret_key, secret_rule, heartbeat_interval} 或 None"""
    ts = _now_ms()
    sign_payload = {
        "platform": "web",
        "parent_id": parent_id,
        "area_id": area_id,
        "seq_id": seq,
        "room_id": room_id,
        "buvid": buvid,
        "uuid": uuid_str,
        "ets": ets,
        "time": heartbeat_interval,
        "ts": ts,
    }
    sign_input = json.dumps(sign_payload, separators=(",", ":"))
    s = _x25kn_sign(sign_input, secret_rule, secret_key)

    form = {
        "s": s,
        "id": json.dumps([parent_id, area_id, seq, room_id], separators=(",", ":")),
        "device": json.dumps([buvid, uuid_str], separators=(",", ":")),
        "ruid": up_id,
        "ets": ets,
        "benchmark": secret_key,
        "time": heartbeat_interval,
        "ts": ts,
        "ua": _X25KN_UA,
        "csrf_token": bili_jct,
        "csrf": bili_jct,
        "visit_id": "",
    }
    resp = _x25kn_post(_X25KN_X_URL, form, sessdata, bili_jct,
                       f"https://live.bilibili.com/{room_id}")
    if resp and resp.get("code") == 0 and resp.get("data"):
        return resp["data"]
    code = (resp or {}).get("code", "?")
    msg = (resp or {}).get("message", str(resp))
    print(f"    ⚠️  X 心跳失败: [{code}] {msg}", file=sys.stderr)
    _log({"type": "x25kn_x_error", "room_id": room_id, "seq": seq,
          "code": code, "message": msg})
    return None



# ──────────────────────────────────────────
# Legacy Heartbeat (fallback, 不涨亲密度)
# ──────────────────────────────────────────

def send_web_heartbeat(room_id: int, sessdata: str, bili_jct: str) -> bool:
    """旧版心跳（fallback），不涨亲密度但维持在线"""
    url = "https://api.live.bilibili.com/User/userOnlineHeart"
    headers = _make_headers(sessdata, bili_jct, f"https://live.bilibili.com/{room_id}")
    data = {"csrf": bili_jct, "csrf_token": bili_jct}
    resp = _post_form(url, data, headers)
    return resp is not None and resp.get("code") == 0


# ──────────────────────────────────────────
# Fan Medal
# ──────────────────────────────────────────

def wear_medal(medal_id: int, sessdata: str, bili_jct: str) -> bool:
    url = "https://api.live.bilibili.com/xlive/web-room/v1/fansMedal/wear"
    headers = _make_headers(sessdata, bili_jct)
    data = {"medal_id": str(medal_id), "csrf": bili_jct, "csrf_token": bili_jct}
    resp = _post_form(url, data, headers)
    return resp is not None and resp.get("code") == 0


def get_my_medals(sessdata: str, bili_jct: str) -> Dict[int, Dict]:
    medals = {}
    page = 1
    while True:
        url = f"https://api.live.bilibili.com/xlive/app-ucenter/v1/fansMedal/panel?page={page}&page_size=50"
        headers = _make_headers(sessdata, bili_jct)
        resp = _get_json(url, headers)
        if not resp or resp.get("code") != 0:
            break
        data = resp.get("data", {})
        for item in (data.get("special_list", []) or []) + (data.get("list", []) or []):
            info = item.get("medal", item)
            target_id = info.get("target_id", 0)
            if target_id:
                medals[target_id] = {
                    "medal_id": info.get("medal_id"),
                    "medal_name": info.get("medal_name", ""),
                    "level": info.get("level", 0),
                    "today_intimacy": info.get("today_feed", info.get("today_intimacy", 0)),
                    "day_limit": info.get("day_limit", 0),
                }
        total_page = data.get("page_info", {}).get("total_page", 1)
        if page >= total_page:
            break
        page += 1
    return medals


# ──────────────────────────────────────────
# Watch Loop
# ──────────────────────────────────────────

def watch_room(member: Dict, sessdata: str, bili_jct: str,
               duration_min: int = WATCH_MINUTES, interval: int = HEARTBEAT_INTERVAL,
               until_offline: bool = False, title: str = "") -> Dict:
    """对一个直播间进行 X25Kn 心跳挂机。until_offline=True 时持续到下播为止。"""
    room_id = member["room"]
    up_id = member["uid"]
    name = member["name"]

    entered = enter_room(room_id, sessdata, bili_jct)
    if not entered:
        return {"name": name, "room": room_id, "success": False, "error": "进入直播间失败",
                "beats_ok": 0, "beats_total": 0, "minutes": 0}

    # 获取房间区域信息
    room_info = _get_room_info(room_id, sessdata, bili_jct)
    area_id = room_info.get("area_id", 0) if room_info else 0
    parent_area_id = room_info.get("parent_area_id", 0) if room_info else 0

    # 准备 X25Kn 协议所需的 LIVE_BUVID
    buvid = _ensure_live_buvid(sessdata, bili_jct)
    if not buvid:
        print(f"    ⚠️  无法获取 LIVE_BUVID，亲密度可能无法结算", file=sys.stderr)
        buvid = _random_string(37).upper()  # fallback
    uuid_str = str(uuid.uuid4())

    # E：进入房间，拿到首份 secret_key/secret_rule
    e_data = x25kn_enter_room(room_id, parent_area_id, area_id, up_id,
                              buvid, uuid_str, sessdata, bili_jct)
    if not e_data:
        return {"name": name, "room": room_id, "success": False, "error": "X25Kn E 失败",
                "beats_ok": 0, "beats_total": 0, "minutes": 0}
    secret_key = e_data["secret_key"]
    secret_rule = e_data["secret_rule"]
    ets = e_data["timestamp"]
    heartbeat_interval = int(e_data.get("heartbeat_interval") or interval)
    if heartbeat_interval < 5 or heartbeat_interval > 300:
        heartbeat_interval = HEARTBEAT_INTERVAL
    protocol_seq = 0
    last_response_at = time.monotonic()
    print(f"    📡 X25Kn E 已通过，rule={secret_rule}", file=sys.stderr)

    def _restart_chain() -> bool:
        """X 失败后重新发送 E，避免用过期时间戳连续失败。"""
        nonlocal secret_key, secret_rule, ets
        nonlocal heartbeat_interval, protocol_seq, last_response_at
        restarted = x25kn_enter_room(
            room_id, parent_area_id, area_id, up_id,
            buvid, uuid_str, sessdata, bili_jct,
        )
        last_response_at = time.monotonic()
        if not restarted:
            return False
        secret_key = restarted["secret_key"]
        secret_rule = restarted["secret_rule"]
        ets = restarted["timestamp"]
        heartbeat_interval = int(
            restarted.get("heartbeat_interval") or HEARTBEAT_INTERVAL
        )
        if heartbeat_interval < 5 or heartbeat_interval > 300:
            heartbeat_interval = HEARTBEAT_INTERVAL
        protocol_seq = 0
        print("    🔄 已重建 X25Kn 心跳链", file=sys.stderr)
        return True

    def _do_x_beat():
        """按服务端时间窗口发一次 X，并递推下一轮协议状态。"""
        nonlocal secret_key, secret_rule, ets
        nonlocal heartbeat_interval, protocol_seq, last_response_at

        _wait_for_heartbeat_window(last_response_at, heartbeat_interval)

        next_seq = protocol_seq + 1
        result = x25kn_heartbeat(
            room_id, parent_area_id, area_id, up_id, next_seq,
            buvid, uuid_str, ets, secret_key, secret_rule,
            heartbeat_interval, sessdata, bili_jct,
        )
        last_response_at = time.monotonic()
        if result:
            protocol_seq = next_seq
            secret_key = result.get("secret_key", secret_key)
            secret_rule = result.get("secret_rule", secret_rule)
            ets = result.get("timestamp", ets)
            next_interval = int(
                result.get("heartbeat_interval") or heartbeat_interval
            )
            if 5 <= next_interval <= 300:
                heartbeat_interval = next_interval
            _log({
                "type": "x25kn_x_success",
                "room_id": room_id,
                "seq": protocol_seq,
                "interval": heartbeat_interval,
            })
        else:
            _restart_chain()
        return result

    print(
        f"    📱 X25Kn 心跳已就绪（服务端间隔 {heartbeat_interval}s，看播涨亲密度）",
        file=sys.stderr,
    )

    if until_offline:
        title_str = f"「{title}」" if title else ""
        start_clock = time.strftime("%H:%M")
        _notify(f"🔴 **{name}** 开播啦！{title_str}\n开始时间：{start_clock}，自动挂机中...")
        print(f"    💬 发送弹幕点亮粉丝牌...", file=sys.stderr)
        light_up_medal(room_id, sessdata, bili_jct)
        print(f"    ⏱  开始挂机直到下播（每 {interval}s 心跳一次）...", file=sys.stderr)
        beats_ok = 0
        beat_num = 0
        consecutive_fail = 0
        start_time = time.time()
        while True:
            beat_num += 1
            result = _do_x_beat()
            if result:
                beats_ok += 1
                consecutive_fail = 0
            else:
                consecutive_fail += 1

            elapsed_min = int((time.time() - start_time) / 60)
            if beat_num % 5 == 0:
                print(f"    💓 心跳 {beat_num}(X25Kn)  已挂 {elapsed_min} 分钟  ok:{beats_ok}", file=sys.stderr)
            status = _get_single_room_status(room_id, sessdata, bili_jct)
            if status and status.get("live_status") != 1:
                elapsed_min = int((time.time() - start_time) / 60)
                end_clock = time.strftime("%H:%M")
                h, m = divmod(elapsed_min, 60)
                dur_str = f"{h}小时{m}分钟" if h else f"{m}分钟"
                _notify(f"📴 **{name}** 下播了\n直播时长：{start_clock} - {end_clock}（{dur_str}）")
                print(f"    📴 检测到下播，共挂机 {elapsed_min} 分钟", file=sys.stderr)
                return {
                    "name": name, "room": room_id,
                    "success": beats_ok > 0,
                    "beats_ok": beats_ok, "beats_total": beat_num,
                    "minutes": elapsed_min, "stopped_reason": "offline",
                    "x25kn": True,
                }
            if consecutive_fail >= 10:
                elapsed_min = int((time.time() - start_time) / 60)
                print(f"    ❌ 连续 {consecutive_fail} 次心跳失败，退出", file=sys.stderr)
                return {
                    "name": name, "room": room_id, "success": beats_ok > 0,
                    "beats_ok": beats_ok, "beats_total": beat_num,
                    "minutes": elapsed_min, "stopped_reason": "error",
                    "x25kn": True,
                }
    else:
        total_beats = (duration_min * 60) // heartbeat_interval
        print(f"    ⏱  开始挂机 {duration_min} 分钟（每 {heartbeat_interval}s 心跳一次，共 {total_beats} 次）...",
              file=sys.stderr)
        beats_ok = 0
        for i in range(total_beats):
            result = _do_x_beat()
            if result:
                beats_ok += 1
            elapsed_min = ((i + 1) * heartbeat_interval) // 60
            if (i + 1) % 5 == 0 or i == total_beats - 1:
                print(f"    💓 心跳 {i+1}/{total_beats}(X25Kn)  已挂 {elapsed_min} 分钟", file=sys.stderr)

        actual_minutes = (total_beats * heartbeat_interval) // 60
        return {
            "name": name, "room": room_id,
            "success": beats_ok > 0,
            "beats_ok": beats_ok, "beats_total": total_beats,
            "minutes": actual_minutes, "x25kn": True,
        }


def format_output(live_results: List[Dict], offline_members: List[str],
                  medal_info: Dict[int, Dict], members: List[Dict]) -> str:
    lines = ["🌟 A-SOUL 直播心跳挂机结果", ""]

    if live_results:
        lines.append("  📺 在播成员：")
        for r in live_results:
            uid = next((m["uid"] for m in members if m["name"] == r["name"]), 0)
            medal = medal_info.get(uid)
            medal_str = f"  🏅{medal['medal_name']}Lv{medal['level']}" if medal else ""
            intimacy_str = ""
            if medal:
                intimacy_str = f"  今日亲密度:{medal['today_intimacy']}"
            mode_str = "X25Kn" if r.get("x25kn") else "旧版"

            if r["success"]:
                delta = r.get("intimacy_delta")
                delta_str = f"  亲密度:+{delta}" if isinstance(delta, int) else ""
                lines.append(
                    f"    ✅ {r['name']}{medal_str}  — 挂机 {r['minutes']}min"
                    f"  💓{r['beats_ok']}/{r['beats_total']}({mode_str})"
                    f"{intimacy_str}{delta_str}"
                )
            else:
                lines.append(f"    ❌ {r['name']}  — {r.get('error', '心跳失败')}")

    if offline_members:
        lines.append("")
        lines.append(f"  💤 未开播：{', '.join(offline_members)}")

    lines.append("")
    if live_results:
        ok_count = sum(1 for r in live_results if r["success"])
        lines.append(f"📊 在播 {len(live_results)} 人，挂机成功 {ok_count} 人")
    else:
        lines.append("ℹ️  当前没有成员在播，本次跳过")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="A-SOUL 直播心跳挂机（检测开播 → XL心跳涨亲密度）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 检测所有成员，在播的自动挂机 25 分钟
  python3 heartbeat.py

  # 只检测嘉然和贝拉
  python3 heartbeat.py --members 嘉然,贝拉

  # 只检测开播状态，不挂机
  python3 heartbeat.py --check-only

  # 自定义挂机时长
  python3 heartbeat.py --duration 30
""",
    )
    parser.add_argument("--members", help="指定成员（逗号分隔）")
    parser.add_argument("--duration", type=int, default=WATCH_MINUTES,
                        help=f"挂机时长（分钟，默认 {WATCH_MINUTES}）")
    parser.add_argument("--interval", type=int, default=HEARTBEAT_INTERVAL,
                        help=f"心跳间隔（秒，默认 {HEARTBEAT_INTERVAL}）")
    parser.add_argument("--until-offline", action="store_true",
                        help="持续挂机直到主播下播（忽略 --duration）")
    parser.add_argument("--sessdata", help="SESSDATA cookie")
    parser.add_argument("--bili-jct", help="bili_jct cookie")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--check-only", action="store_true", help="只检测开播状态，不挂机")
    args = parser.parse_args()

    sessdata = args.sessdata
    bili_jct = args.bili_jct
    if not sessdata or not bili_jct:
        saved = load_cookies()
        if saved:
            sessdata = saved["SESSDATA"]
            bili_jct = saved["bili_jct"]
        else:
            print("❌ 没有找到 Cookie。")
            sys.exit(1)

    targets = MEMBERS
    if args.members:
        names = [n.strip() for n in args.members.split(",")]
        targets = [m for m in MEMBERS if m["name"] in names]
        if not targets:
            print(f"❌ 未找到成员: {args.members}")
            sys.exit(1)

    print("  📡 正在检测直播状态...", file=sys.stderr)
    room_ids = [m["room"] for m in targets]
    statuses = check_live_status(room_ids, sessdata, bili_jct)

    live_members = []
    offline_names = []
    for m in targets:
        status = statuses.get(m["room"], {})
        if status.get("live_status") == 1:
            live_members.append(m)
            title = status.get("title", "")
            print(f"  🔴 {m['name']} 正在直播：{title}", file=sys.stderr)
        else:
            offline_names.append(m["name"])
            print(f"  ⚫ {m['name']} 未开播", file=sys.stderr)

    _log({
        "type": "check",
        "live": [{"name": m["name"], "title": statuses.get(m["room"], {}).get("title", "")} for m in live_members],
        "offline": offline_names,
    })

    if args.check_only:
        if args.json:
            result = {
                "live": [{"name": m["name"], "room": m["room"],
                          "title": statuses.get(m["room"], {}).get("title", "")}
                         for m in live_members],
                "offline": offline_names,
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            if live_members:
                print(f"\n🔴 在播：{', '.join(m['name'] for m in live_members)}")
            if offline_names:
                print(f"⚫ 未开播：{', '.join(offline_names)}")
        return

    if not live_members:
        print("\nℹ️  当前没有成员在播，本次跳过")
        return

    login_valid, login_message = check_login(sessdata, bili_jct)
    if not login_valid:
        print(
            f"\n❌ B 站登录态无效（{login_message}），本次不启动挂机。",
            file=sys.stderr,
        )
        _log({"type": "auth_error", "message": login_message})
        sys.exit(1)

    print("  🏅 正在获取粉丝牌信息...", file=sys.stderr)
    medals = get_my_medals(sessdata, bili_jct)

    if args.until_offline:
        _LOCK_DIR.mkdir(exist_ok=True)

    live_results = []
    for i, m in enumerate(live_members):
        lock_file = _LOCK_DIR / f"{m['room']}.lock" if args.until_offline else None

        if lock_file and lock_file.exists():
            # 检查锁文件是否过期（进程已不存在）
            lock_pid = lock_file.read_text().strip()
            lock_valid = False
            try:
                if lock_pid:
                    os.kill(int(lock_pid), 0)  # 检查进程是否存在
                    lock_valid = True
            except (OSError, ValueError, ProcessLookupError):
                pass
            
            if lock_valid:
                print(f"\n  ⏭  [{i+1}/{len(live_members)}] {m['name']} 已有挂机在运行，跳过", file=sys.stderr)
                live_results.append({"name": m["name"], "room": m["room"],
                                      "success": True, "skipped": True,
                                      "beats_ok": 0, "beats_total": 0, "minutes": 0})
                continue
            else:
                # 清理过期的锁文件
                print(f"\n  🧹 [{i+1}/{len(live_members)}] {m['name']} 清理过期锁文件", file=sys.stderr)
                lock_file.unlink()

        print(f"\n  📺 [{i+1}/{len(live_members)}] {m['name']} 直播间 {m['room']}", file=sys.stderr)

        if m["uid"] in medals:
            medal = medals[m["uid"]]
            worn = wear_medal(medal["medal_id"], sessdata, bili_jct)
            if worn:
                print(f"    🏅 已佩戴 {medal['medal_name']}Lv{medal['level']}", file=sys.stderr)
            time.sleep(1)

        if lock_file:
            lock_file.write_text(str(os.getpid()))
        _log({"type": "watch_start", "member": m["name"], "room": m["room"]})
        live_title = statuses.get(m["room"], {}).get("title", "")
        result = {
            "name": m["name"],
            "room": m["room"],
            "success": False,
            "error": "挂机进程异常退出",
            "beats_ok": 0,
            "beats_total": 0,
            "minutes": 0,
        }
        try:
            result = watch_room(m, sessdata, bili_jct,
                                duration_min=args.duration, interval=args.interval,
                                until_offline=args.until_offline, title=live_title)
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            print(f"    ❌ 挂机异常: {result['error']}", file=sys.stderr)
            _log({
                "type": "watch_exception",
                "member": m["name"],
                "room": m["room"],
                "error": result["error"],
            })
        finally:
            if lock_file and lock_file.exists():
                lock_file.unlink()

        before_intimacy = medals.get(m["uid"], {}).get("today_intimacy")
        refreshed_medals = get_my_medals(sessdata, bili_jct)
        after_intimacy = refreshed_medals.get(m["uid"], {}).get("today_intimacy")
        if before_intimacy is not None and after_intimacy is not None:
            result["intimacy_before"] = before_intimacy
            result["intimacy_after"] = after_intimacy
            result["intimacy_delta"] = after_intimacy - before_intimacy
            medals[m["uid"]] = refreshed_medals[m["uid"]]

        _log({"type": "watch_end", "member": m["name"], "room": m["room"],
              "minutes": result.get("minutes", 0), "beats_ok": result.get("beats_ok", 0),
              "intimacy_before": result.get("intimacy_before"),
              "intimacy_after": result.get("intimacy_after"),
              "intimacy_delta": result.get("intimacy_delta")})

        live_results.append(result)

        if i < len(live_members) - 1:
            time.sleep(3)

    if args.json:
        print(json.dumps({"results": live_results, "offline": offline_names},
                          ensure_ascii=False, indent=2))
    else:
        print("")
        print(format_output(live_results, offline_names, medals, targets))


if __name__ == "__main__":
    main()
