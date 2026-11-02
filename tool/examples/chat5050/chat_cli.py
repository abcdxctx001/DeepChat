#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chat5050 无头命令行客户端
=========================

依赖：requests（pip install requests）

示例：
    python chat_cli.py login 小明
    python chat_cli.py send "大家好！"
    python chat_cli.py send -f ./cat.png --caption "看这只猫"
    python chat_cli.py history -l 30
    python chat_cli.py watch
    python chat_cli.py users

默认服务器 http://127.0.0.1:5050，昵称/服务器会保存到 ~/.chat5050.json。
也可以通过环境变量 CHAT5050_SERVER / CHAT5050_NICKNAME 或 -s / -n 参数指定。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

try:
    import requests
except ImportError:  # pragma: no cover
    print("缺少依赖 requests，请先运行：pip install requests", file=sys.stderr)
    raise SystemExit(1)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DEFAULT_SERVER = os.environ.get("CHAT5050_SERVER", "http://127.0.0.1:5050")
CONFIG_PATH = Path(
    os.environ.get("CHAT5050_CONFIG", Path.home() / ".chat5050.json")
)
TIMEOUT = 20.0

BASE = DEFAULT_SERVER.rstrip("/")
NICK = ""


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------

def die(message: str, code: int = 1) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(code)


def load_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(config: dict) -> None:
    try:
        CONFIG_PATH.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        die(f"无法写入配置文件 {CONFIG_PATH}：{exc}")


def request_json(method: str, path: str, timeout: float = TIMEOUT, **kwargs):
    url = BASE + path
    try:
        resp = requests.request(method, url, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        die(f"无法连接服务器 {url}\n  {exc}")
    try:
        data = resp.json()
    except ValueError:
        die(f"服务器返回了非 JSON 内容（HTTP {resp.status_code}）")
    if resp.status_code >= 400 or not isinstance(data, dict) or not data.get("ok"):
        error = (data or {}).get("error") or {}
        message = error.get("message") or f"请求失败（HTTP {resp.status_code}）"
        code = error.get("code") or "error"
        die(f"{message}  [{code}, HTTP {resp.status_code}]")
    return data


def require_nickname() -> str:
    if not NICK:
        die(
            "还没有昵称。先运行 `python chat_cli.py login 你的昵称`，"
            "或加参数 `-n 你的昵称`。"
        )
    return NICK


def human_time(iso: str) -> str:
    try:
        return (
            datetime.fromisoformat(iso.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S")
        )
    except (ValueError, AttributeError):
        return iso or ""


def format_message(message: dict) -> str:
    stamp = human_time(message.get("created_at", ""))
    text = (message.get("text") or "").strip()
    parts = []
    if text:
        parts.append(text)
    attachment = message.get("attachment")
    if attachment:
        label = "图片" if attachment.get("is_image") else "文件"
        download = attachment.get("download_url") or attachment.get("url") or ""
        url = f"{BASE}{download}"
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}nickname={quote(NICK)}"
        size = attachment.get("size_human") or f"{attachment.get('size')} B"
        parts.append(f"[{label}] {attachment.get('name')} ({size}) {url}")
    body = " ".join(parts) if parts else "（空消息）"
    lines = body.splitlines() or [""]
    head = f"[{stamp}] {message.get('nickname', '?')}: {lines[0]}"
    indent = " " * (len(stamp) + 4)
    for line in lines[1:]:
        head += f"\n{indent}{line}"
    return head


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

def cmd_login(args) -> None:
    target = (args.nick or NICK or "").strip()
    if not target:
        die("请提供昵称，例如：python chat_cli.py login 小明")
    data = request_json("POST", "/api/login", json={"nickname": target})
    nickname = data.get("nickname", target)
    save_config({"server": BASE, "nickname": nickname})
    group = data.get("group") or {}
    print(f"已进入「{group.get('name', '大厅')}」，昵称：{nickname}")
    print(f"服务器：{BASE}")
    print(f"配置已保存：{CONFIG_PATH}")


def cmd_send(args) -> None:
    nickname = require_nickname()
    text = " ".join(args.text).strip()
    file_path = args.file

    if not text and not file_path:
        die("消息内容不能为空：输入文字，或用 -f/--file 指定文件")

    if file_path:
        path = Path(file_path).expanduser()
        if not path.is_file():
            die(f"文件不存在：{path}")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        caption = (args.caption or text).strip()
        with path.open("rb") as handle:
            data = request_json(
                "POST",
                "/api/upload",
                data={"nickname": nickname, "text": caption},
                files={"file": (path.name, handle, mime)},
                timeout=300.0,
            )
    else:
        data = request_json(
            "POST",
            "/api/messages",
            json={"nickname": nickname, "text": text},
        )

    print(format_message(data["message"]))


def cmd_history(args) -> None:
    nickname = require_nickname()
    params = {"nickname": nickname, "limit": max(1, min(args.limit, 200))}
    if args.before:
        params["before_id"] = args.before
    data = request_json("GET", "/api/messages", params=params)
    messages = data.get("messages") or []
    if not messages:
        print("（暂无消息）")
        return
    for message in messages:
        print(format_message(message))
    if data.get("has_more"):
        print(
            f"—— 还有更早的消息，使用 --before {data.get('oldest_id')} 继续查看",
            file=sys.stderr,
        )


def cmd_watch(args) -> None:
    nickname = require_nickname()
    if args.from_start:
        after = 0
    elif args.after is not None:
        after = args.after
    else:
        latest = request_json(
            "GET", "/api/messages", params={"nickname": nickname, "limit": 1}
        )
        messages = latest.get("messages") or []
        after = messages[-1]["id"] if messages else 0
        print(f"从最新位置开始监听（after_id={after}）…", file=sys.stderr)

    print("已连接，按 Ctrl+C 停止监听。", file=sys.stderr)
    try:
        while True:
            data = request_json(
                "GET",
                "/api/messages",
                params={
                    "nickname": nickname,
                    "after_id": after,
                    "limit": 100,
                    "wait": 25,
                },
                timeout=45.0,
            )
            for message in data.get("messages") or []:
                print(format_message(message), flush=True)
                after = max(after, int(message["id"]))
    except KeyboardInterrupt:
        print("\n已停止监听。", file=sys.stderr)


def cmd_users(args) -> None:
    nickname = require_nickname()
    data = request_json("GET", "/api/presence", params={"nickname": nickname})
    people = data.get("online") or []
    print(f"在线 {len(people)} 人")
    for person in people:
        print(f"  · {person.get('nickname')}")


def cmd_me(args) -> None:
    nickname = require_nickname()
    data = request_json("GET", "/api/me", params={"nickname": nickname})
    group = data.get("group") or {}
    print(f"昵称：{data.get('nickname')}")
    print(f"群组：{group.get('name', '大厅')}")
    print(f"在线：{group.get('online_count', 0)} 人")
    print(f"消息总数：{group.get('message_count', 0)}")


def cmd_health(args) -> None:
    data = request_json("GET", "/api/health")
    data.pop("ok", None)
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-s", "--server", default=argparse.SUPPRESS, metavar="URL",
        help=f"服务器地址（默认 {DEFAULT_SERVER}）",
    )
    common.add_argument(
        "-n", "--nickname", default=argparse.SUPPRESS, metavar="NAME",
        help="昵称（默认读取配置文件）",
    )

    parser = argparse.ArgumentParser(
        prog="chat_cli.py",
        description="Chat5050 无头命令行客户端：发送文字 / 图片 / 文件，监听大厅消息。",
        epilog=(
            "示例：\n"
            "  python chat_cli.py login 小明\n"
            "  python chat_cli.py send \"大家好\"\n"
            "  python chat_cli.py send -f photo.png --caption 看这张图\n"
            "  python chat_cli.py watch --from-start\n"
            "  python chat_cli.py history -l 30\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("login", parents=[common], help="验证并保存昵称")
    p.add_argument("nick", nargs="?", metavar="NICKNAME", help="要使用的昵称")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("send", parents=[common], help="发送文字或上传文件")
    p.add_argument("text", nargs="*", help="文字内容（可省略，改用 --file）")
    p.add_argument("-f", "--file", metavar="PATH", help="要上传的图片或文件")
    p.add_argument("--caption", default="", metavar="TEXT", help="文件附带的说明文字")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("history", parents=[common], help="查看历史消息")
    p.add_argument("-l", "--limit", type=int, default=20, help="条数，默认 20")
    p.add_argument("--before", type=int, default=None, metavar="ID", help="查看该 id 之前的消息")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("watch", parents=[common], help="持续监听新消息")
    p.add_argument("--after", type=int, default=None, metavar="ID", help="从该 id 之后开始")
    p.add_argument("--from-start", action="store_true", help="从第一条消息开始")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("users", parents=[common], help="查看在线成员")
    p.set_defaults(func=cmd_users)

    p = sub.add_parser("me", parents=[common], help="查看当前身份与群信息")
    p.set_defaults(func=cmd_me)

    p = sub.add_parser("health", parents=[common], help="查看服务器健康状态")
    p.set_defaults(func=cmd_health)

    return parser


def main() -> None:
    global BASE, NICK
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help()
        return

    config = load_config()
    base = (
        getattr(args, "server", None)
        or config.get("server")
        or DEFAULT_SERVER
    ).strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    nickname = getattr(args, "nickname", None) or config.get("nickname") or ""
    BASE = base
    NICK = str(nickname).strip()

    args.func(args)


if __name__ == "__main__":
    main()
