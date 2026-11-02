#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""牛哞哞 —— chat5050 大厅里的 cowsay 机器人。

它用 chat5050 的 headless API 常驻在大厅里：你说话，它把话塞进气泡里，再配一只小动物。

    python cowsay_bot.py                  # 默认连接 http://127.0.0.1:5050
    python cowsay_bot.py --nick 哞哞      # 换个名字
    python cowsay_bot.py --mode mention   # 只在被叫到时才回话（安静模式）
    python cowsay_bot.py --face cat --width 30

命令（在聊天框里发给它）：
    /help                看帮助
    /牛 你好             让奶牛说「你好」
    /猫 你好             换猫来说（也可以 /企鹅 /幽灵 /兔子 /龙）
    /想 今晚吃什么       变成思考泡泡
    /形象 cat            切换默认形象
    /宽度 30             调整气泡宽度（按显示宽度算，中文算 2 列）
    /闭嘴  /说话         关掉/打开自动回话
    /谁                  看当前形象与设置
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import datetime

import requests

from cowsay_art import FACES, render, faces_line, resolve_face, looks_like_art

DEFAULT_SERVER = os.environ.get("CHAT_SERVER", "http://127.0.0.1:5050")
DEFAULT_NICK = os.environ.get("CHAT_BOT_NICK", "牛哞哞")

# 不理这些账号，避免「机器人 × 机器人」无限复读
DEFAULT_IGNORE = ("DeepSeek", "deepseek")

# 被叫到的触发词
CALL_NAMES = ("牛", "哞", "cow", "moo", "奶牛")

# 偶尔加一句小尾巴，25% 概率，避免每句都加显得吵
QUIPS = [
    "（甩了甩尾巴）",
    "（胃里反刍了一下）",
    "（面无表情地嚼草）",
    "（尾巴打了个问号）",
    "（歪头）",
    "哞。",
]

HELP_TEXT = """我是牛哞哞，住在 5050 大厅的奶牛。

直接跟我说话，我就把你的话塞进气泡里再配一只小动物。
想更有趣，可以这么使唤我：

  /牛 你好          让奶牛说「你好」
  /猫 你好          换猫来说（/企鹅 /幽灵 /兔子 /龙）
  /想 今晚吃什么     变成思考泡泡
  /形象 cat         切换默认形象
  /宽度 30          调整气泡宽度
  /闭嘴   /说话      关掉 / 打开自动回话
  /谁               看看我现在是什么设定

可用形象：%s
（提示：你也可以在网页里直接 @我，或者带上「牛」「哞」两个字。）""" % faces_line()


LOG_PATH = os.environ.get("CHAT_BOT_LOG", "")


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if LOG_PATH:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


class CowsayBot:
    def __init__(self, server: str, nick: str, mode: str, face: str, width: int,
                 ignore: tuple[str, ...] = ()):
        self.server = server.rstrip("/")
        self.nick = nick
        self.mode = mode                 # echo | mention | off
        self.face = resolve_face(face)
        self.width = width
        self.cursor = 0
        self.last_reply = 0.0
        self.prev_mode = "echo" if mode == "off" else mode
        self.ignore = tuple(n for n in ignore if n)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "cowsay-bot/1.0"

    # -- 基础 HTTP ---------------------------------------------------------

    def api(self, method: str, path: str, **kwargs):
        url = f"{self.server}{path}"
        kwargs.setdefault("timeout", 40)
        return self.session.request(method, url, **kwargs)

    def wait_ready(self) -> None:
        while True:
            try:
                resp = self.api("GET", "/api/health", timeout=5)
                if resp.ok:
                    data = resp.json()
                    log(f"已连上 {self.server}（{data.get('app')}，{data.get('message_count')} 条历史消息）")
                    return
            except requests.RequestException as exc:
                log(f"等不到服务器：{exc.__class__.__name__}，3 秒后重试")
            time.sleep(3)

    def jump_to_latest(self) -> None:
        """启动时把游标推到最新，别去回复历史消息。"""
        try:
            resp = self.api(
                "GET", "/api/messages",
                params={"nickname": self.nick, "limit": 1},
            )
            data = resp.json()
            self.cursor = int(data.get("latest_id") or 0)
            log(f"从第 {self.cursor} 条消息之后开始听你说话")
        except Exception as exc:  # noqa: BLE001
            log(f"读取历史失败：{exc}")

    # -- 发消息 -----------------------------------------------------------

    def say(self, text: str) -> bool:
        try:
            resp = self.api(
                "POST", "/api/messages",
                json={"nickname": self.nick, "text": text},
            )
        except requests.RequestException as exc:
            log(f"发送失败：{exc.__class__.__name__}")
            return False

        if resp.status_code == 429:
            log("被限流了，歇 8 秒")
            time.sleep(8)
            return False
        if not resp.ok:
            log(f"发送失败：HTTP {resp.status_code} {resp.text[:120]}")
            return False
        msg = resp.json().get("message", {})
        log(f"已回复 #{msg.get('id')}（{len(text)} 字）")
        return True

    # -- 业务逻辑 ---------------------------------------------------------

    def reply_art(self, text: str, face: str | None = None, think: bool = False) -> None:
        art = render(text, face=face or self.face, width=self.width, think=think)
        gap = time.time() - self.last_reply
        if gap < 1.2:
            time.sleep(1.2 - gap)
        if self.say(art):
            self.last_reply = time.time()
            # 偶尔补一句碎碎念
            if random.random() < 0.25:
                time.sleep(0.6)
                self.say(random.choice(QUIPS))

    def handle_command(self, text: str) -> bool:
        """处理 / 开头的命令，返回 True 表示已消化。"""
        body = text[1:].strip()
        if not body:
            return False
        head, _, rest = body.partition(" ")
        head = head.strip()
        rest = rest.strip()

        if head in ("help", "帮助", "?", "？"):
            self.say(HELP_TEXT)
            return True

        if head in ("形象", "face", "换脸"):
            if not rest:
                self.say(f"我现在是「{FACES[self.face]['label']}」。可用形象：{faces_line()}")
                return True
            self.face = resolve_face(rest)
            self.reply_art(f"好，从现在起我是{FACES[self.face]['label']}。", self.face)
            return True

        if head in ("宽度", "width"):
            try:
                self.width = max(12, min(80, int(rest)))
                self.reply_art(f"气泡宽度调到 {self.width} 了。")
            except ValueError:
                self.say("宽度要写个数字，比如 /宽度 30")
            return True

        if head in ("闭嘴", "mute", "安静"):
            if self.mode != "off":
                self.prev_mode = self.mode
            self.mode = "off"
            self.say("好，我先闭嘴。想说回来就发 /说话 或者叫我一声。")
            return True

        if head in ("说话", "unmute", "上线"):
            self.mode = self.prev_mode
            self.reply_art("我回来了。")
            return True

        if head in ("谁", "who", "状态", "status"):
            mode_cn = {"echo": "每句都接话", "mention": "只有被叫到才说话", "off": "闭嘴中"}[self.mode]
            self.say(
                f"我是 {self.nick}，形象「{FACES[self.face]['label']}」，"
                f"气泡宽度 {self.width}，当前模式：{mode_cn}。"
            )
            return True

        if head in ("想", "think", "思考"):
            self.reply_art(rest or "……", think=True)
            return True

        # /牛 你好、/猫 你好 …
        face = resolve_face(head)
        known = head.lower() in ("cow", "cat", "penguin", "ghost", "rabbit", "dragon",
                                 "牛", "奶牛", "猫", "猫咪", "企鹅", "幽灵", "兔子", "龙", "小龙",
                                 "兔", "鬼", "喵")
        if known:
            self.reply_art(rest or "哞～", face)
            return True

        return False

    def handle(self, message: dict) -> None:
        nick = message.get("nickname") or ""
        text = (message.get("text") or "").strip()
        if not nick or nick == self.nick or not text:
            return
        if any(nick == name or nick.startswith(name) for name in self.ignore):
            return
        # 别人也是 ASCII 画（比如另一个机器人），就别再复读一遍
        if looks_like_art(text):
            return
        if message.get("attachment") and not text:
            return

        if text.startswith("/"):
            if self.handle_command(text):
                return
            self.say(f"我不认识「{text.split()[0]}」这个命令，发 /help 看看我会什么。")
            return

        called = any(name in text.lower() for name in CALL_NAMES) or self.nick in text
        if self.mode == "off" and not called:
            return
        if self.mode == "mention" and not called:
            return

        self.reply_art(text)

    # -- 主循环 -----------------------------------------------------------

    def run(self) -> None:
        self.wait_ready()
        self.jump_to_latest()
        log(f"我是 {self.nick}，形象「{FACES[self.face]['label']}」，模式 {self.mode}，"
            f"宽度 {self.width}。可以开始聊天了（Ctrl+C 退出）")
        backoff = 1.0
        while True:
            try:
                resp = self.api(
                    "GET", "/api/messages",
                    params={
                        "nickname": self.nick,
                        "after_id": self.cursor,
                        "limit": 50,
                        "wait": 25,
                    },
                )
                if resp.status_code == 429:
                    time.sleep(5)
                    continue
                if not resp.ok:
                    log(f"拉取消息失败：HTTP {resp.status_code}")
                    time.sleep(3)
                    continue
                data = resp.json()
                backoff = 1.0
                for message in data.get("messages", []):
                    mid = int(message.get("id") or 0)
                    if mid > self.cursor:
                        self.cursor = mid
                    try:
                        self.handle(message)
                    except Exception as exc:  # noqa: BLE001
                        log(f"处理消息 {mid} 出错：{exc.__class__.__name__}: {exc}")
            except KeyboardInterrupt:
                log("收到 Ctrl+C，下班。哞～")
                return
            except requests.RequestException as exc:
                log(f"连接中断：{exc.__class__.__name__}，{backoff:.0f} 秒后重连")
                time.sleep(backoff)
                backoff = min(backoff * 2, 15)
            except Exception as exc:  # noqa: BLE001
                log(f"意外错误：{exc.__class__.__name__}: {exc}")
                time.sleep(2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="chat5050 的 cowsay 机器人")
    parser.add_argument("-s", "--server", default=DEFAULT_SERVER, help="服务器地址")
    parser.add_argument("-n", "--nick", default=DEFAULT_NICK, help="机器人昵称")
    parser.add_argument("-m", "--mode", default="echo",
                        choices=["echo", "mention", "off"], help="回话策略")
    parser.add_argument("-f", "--face", default="cow", help="默认形象")
    parser.add_argument("-w", "--width", type=int, default=36, help="气泡宽度（显示列）")
    parser.add_argument("-i", "--ignore", default=",".join(DEFAULT_IGNORE),
                        help="不理会的昵称，逗号分隔")
    args = parser.parse_args(argv)

    ignore = tuple(x.strip() for x in args.ignore.split(",") if x.strip())
    bot = CowsayBot(args.server, args.nick, args.mode, args.face, args.width, ignore)
    try:
        bot.run()
    except KeyboardInterrupt:
        log("收到 Ctrl+C，下班。哞～")
    return 0


if __name__ == "__main__":
    sys.exit(main())
