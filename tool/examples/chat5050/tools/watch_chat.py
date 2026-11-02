#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chat5050 后台消息监听器
=======================

用于让开发者 / 自动化助手实时收到大厅新消息提醒。与 `chat_cli.py watch`
的区别：每条消息只输出一行摘要，适合作为定时任务或日志管道，不会把整段
ASCII 牛打印出来。

默认：
  * 服务器 http://127.0.0.1:5050
  * 昵称   Chat5050 开发者
  * 忽略   牛哞哞（大厅里的自动 cowsay 机器人，避免刷屏）
  * 跳过自己发的消息

示例：
    python -u tools/watch_chat.py                 # 从当前最新位置开始监听
    python -u tools/watch_chat.py --after 79      # 从 id 79 之后开始
    python -u tools/watch_chat.py --once --after 73
    python -u tools/watch_chat.py --include-self --no-default-ignore
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

import requests

DEFAULT_SERVER = os.environ.get("CHAT5050_SERVER", "http://127.0.0.1:5050")
DEFAULT_NICKNAME = "Chat5050 开发者"
DEFAULT_IGNORE = "牛哞哞"


def local_time(iso: str) -> str:
    try:
        return (
            datetime.fromisoformat(iso.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%H:%M:%S")
        )
    except (ValueError, AttributeError):
        return iso or "--:--:--"


def preview(message: dict) -> str:
    text = (message.get("text") or "").replace("\r", "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        body = lines[0]
        if len(lines) > 1:
            body += f" …(+{len(lines) - 1} 行)"
    else:
        body = ""
    if len(body) > 220:
        body = body[:220] + "…"

    attachment = message.get("attachment")
    if attachment:
        kind = "图片" if attachment.get("is_image") else "文件"
        piece = (
            f"[{kind}] {attachment.get('name')} "
            f"({attachment.get('size_human') or attachment.get('size')})"
        )
        body = f"{body} {piece}".strip()
    return body or "（空消息）"


def fetch_latest_id(base: str, nickname: str) -> int:
    response = requests.get(
        base + "/api/messages",
        params={"nickname": nickname, "limit": 1},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    messages = data.get("messages") or []
    return int(data.get("latest_id") or 0) if not messages else int(messages[-1]["id"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Chat5050 新消息监听器")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="服务器地址")
    parser.add_argument("--nickname", default=DEFAULT_NICKNAME, help="监听身份昵称")
    parser.add_argument("--after", type=int, default=None, help="从该消息 id 之后开始")
    parser.add_argument("--once", action="store_true", help="只读取一次积压消息后退出")
    parser.add_argument("--include-self", action="store_true", help="也提醒自己发的消息")
    parser.add_argument(
        "--ignore",
        action="append",
        default=None,
        metavar="NICKNAME",
        help="额外忽略的昵称，可重复",
    )
    parser.add_argument(
        "--no-default-ignore",
        action="store_true",
        help="不再默认忽略 牛哞哞",
    )
    args = parser.parse_args()

    base = args.server.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "http://" + base

    ignored = set()
    if not args.no_default_ignore:
        ignored.add(DEFAULT_IGNORE)
    for name in args.ignore or []:
        if name:
            ignored.add(name)

    try:
        after = args.after
        if after is None:
            after = fetch_latest_id(base, args.nickname)
        print(
            f"watch start: server={base} as={args.nickname} after_id={after}",
            file=sys.stderr,
            flush=True,
        )
    except requests.RequestException as exc:
        print(f"watch init error: {exc}", file=sys.stderr, flush=True)
        return 1

    while True:
        try:
            response = requests.get(
                base + "/api/messages",
                params={
                    "nickname": args.nickname,
                    "after_id": after,
                    "limit": 50,
                    "wait": 0 if args.once else 25,
                },
                timeout=45,
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError((data.get("error") or {}).get("message") or "API error")

            for message in data.get("messages") or []:
                message_id = int(message["id"])
                after = max(after, message_id)
                nickname = message.get("nickname") or "?"
                if nickname in ignored:
                    continue
                if nickname == args.nickname and not args.include_self:
                    continue
                print(
                    f"[{local_time(message.get('created_at', ''))}] "
                    f"#{message_id} {nickname}: {preview(message)}",
                    flush=True,
                )

            if args.once:
                break
        except KeyboardInterrupt:
            break
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            print(f"watch error: {exc}", file=sys.stderr, flush=True)
            time.sleep(3)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
