# -*- coding: utf-8 -*-
"""诊断：读取文件传输助手当前全部消息（独立进程，一次性）。"""
import io
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "..", "wechat_headless_api"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

print("[1] 连接微信 ...", flush=True)
from wx_headless import WeChatHeadless  # noqa: E402

api = WeChatHeadless(default_chat="文件传输助手")
print("[2] 读取消息 ...", flush=True)
msgs = api.get_messages("文件传输助手")
print("[3] 消息总数:", len(msgs), flush=True)
for m in msgs[-8:]:
    print("-", m["sender"], "|", repr(m["content"])[:140], "| timestr=", repr(m["timestr"]), flush=True)
