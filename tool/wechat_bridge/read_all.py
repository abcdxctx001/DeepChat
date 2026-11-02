# -*- coding: utf-8 -*-
"""核查：读取文件传输助手会话的完整消息列表（带重试，独立进程一次性）。"""
import io
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "..", "wechat_headless_api"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

print("[1] 连接微信 ...", flush=True)
from wx_headless import WeChatHeadless  # noqa: E402

api = WeChatHeadless(default_chat="文件传输助手")

ok = False
for i in range(6):
    try:
        msgs = api.get_messages("文件传输助手")
        if msgs:
            print("[3] 第", i + 1, "次尝试 → 共", len(msgs), "条消息", flush=True)
            for m in msgs[-15:]:
                c = m["content"].replace("\n", " / ")[:200]
                print("   -", m["sender"], "|", c, flush=True)
            ok = True
            break
        else:
            print("   第", i + 1, "次尝试 → 读到 0 条，重试 ...", flush=True)
    except Exception as e:
        print("   第", i + 1, "次尝试 → 异常:", e, flush=True)
    time.sleep(2)

if not ok:
    print("[3] 多次尝试后仍未读到消息", flush=True)
