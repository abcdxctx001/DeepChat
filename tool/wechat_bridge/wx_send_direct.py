# -*- coding: utf-8 -*-
"""独立进程直接发送（不经 HTTP 服务；用于链路验证/备用通道）。

用法: python wx_send_direct.py msg.txt [chat]
注意：运行时会与 serve 服务竞争微信窗口，仅建议在无 serve 或验证链路时使用。
"""
import io
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "..", "wechat_headless_api"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

print("[1/3] 连接微信 ...", flush=True)
from wx_headless import WeChatHeadless  # noqa: E402

path = sys.argv[1]
chat = sys.argv[2] if len(sys.argv) > 2 else "文件传输助手"
with open(path, encoding="utf-8") as f:
    text = f.read().rstrip("\n")

api = WeChatHeadless(default_chat=chat)
print("[2/3] 发送中 ...", flush=True)
api.send(text, chat=chat)
print("[3/3] 已发送: {} 字 -> {}".format(len(text), chat), flush=True)
