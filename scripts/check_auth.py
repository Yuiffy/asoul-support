#!/usr/bin/env python3
"""Validate Bilibili credentials without printing secret values."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple


_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _load_local_cookies() -> Tuple[Optional[str], Optional[str]]:
    path = Path(__file__).resolve().parent.parent / ".cookies.json"
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    return data.get("SESSDATA"), data.get("bili_jct")


def check_login(sessdata: str, bili_jct: str) -> Tuple[bool, str]:
    request = urllib.request.Request(
        "https://api.bilibili.com/x/web-interface/nav",
        headers={
            "User-Agent": _UA,
            "Cookie": f"SESSDATA={sessdata}; bili_jct={bili_jct}",
            "Referer": "https://www.bilibili.com/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return False, f"认证接口请求失败: {type(exc).__name__}"

    data = body.get("data") or {}
    if body.get("code") == 0 and data.get("isLogin") is True:
        return True, data.get("uname") or "已登录账号"
    return False, f"{body.get('code', '?')} {body.get('message', '账号未登录')}"


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 B 站 Cookie 登录态")
    parser.add_argument("--sessdata")
    parser.add_argument("--bili-jct")
    args = parser.parse_args()

    local_sessdata, local_bili_jct = _load_local_cookies()
    sessdata = args.sessdata or os.environ.get("SESSDATA") or local_sessdata
    bili_jct = args.bili_jct or os.environ.get("BILI_JCT") or local_bili_jct

    if not sessdata or not bili_jct:
        print("❌ 缺少 SESSDATA 或 BILI_JCT。", file=sys.stderr)
        return 1

    valid, message = check_login(sessdata, bili_jct)
    if not valid:
        print(
            f"❌ B 站登录态无效（{message}）。请更新 GitHub Secrets 或本地 .cookies.json。",
            file=sys.stderr,
        )
        return 1

    print(f"✅ B 站登录态有效：{message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
