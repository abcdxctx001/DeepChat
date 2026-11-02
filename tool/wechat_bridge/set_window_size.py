# -*- coding: utf-8 -*-
"""把微信窗口调整到足够大的尺寸（消息渲染完整所需）。

用法: python set_window_size.py [width] [height]
默认 1366x850。当"消息读取不全/漏消息"时执行一次即可。
"""
import io
import sys
import time

import win32gui

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

W = int(sys.argv[1]) if len(sys.argv) > 1 else 1366
H = int(sys.argv[2]) if len(sys.argv) > 2 else 850


def find_wx_windows():
    ret = []

    def cb(h, _):
        try:
            cls = win32gui.GetClassName(h)
            if "Qt" in cls and "QWindowIcon" in cls:
                t = win32gui.GetWindowText(h)
                if t.startswith("微信") and win32gui.IsWindowVisible(h):
                    ret.append(h)
        except Exception:
            pass

    win32gui.EnumWindows(cb, 0)
    return ret


ws = find_wx_windows()
if not ws:
    print("未找到微信窗口（微信未启动？）")
    sys.exit(1)
for h in ws:
    r = win32gui.GetWindowRect(h)
    print("窗口 {}: {}x{} -> {}x{}".format(hex(h), r[2]-r[0], r[3]-r[1], W, H))
    win32gui.MoveWindow(h, 0, 0, W, H, True)
time.sleep(1)
print("完成")
