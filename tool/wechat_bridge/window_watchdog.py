# -*- coding: utf-8 -*-
"""微信窗口尺寸守护：保持窗口足够大，确保长消息渲染/读取完整。

- 目标尺寸 1366x850（适配本机屏幕 1368x912）
- 每 15 秒检查一次；窗口显著变小（宽<800 或 高<700）时自动恢复
- 调整事件写入 logs/window_watchdog.log（stdout 限频输出，避免通知轰炸）
- 背景：微信 4.x + UIA 下，窗口太小会导致消息列表渲染不全 → 漏读消息
"""
import io
import os
import sys
import time

import win32gui

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "window_watchdog.log")

TARGET_W, TARGET_H = 1366, 850
CHECK_INTERVAL = 15

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_last_print = [0.0]


def log(msg, to_console=True):
    line = "{} {}".format(time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    now = time.time()
    if to_console and now - _last_print[0] > 60:
        print(line, flush=True)
        _last_print[0] = now


def find_wx_windows():
    """只找微信主窗口：
    - 类名含 Qt 与 QWindowIcon（排除 ToolSaveBits 等工具窗）
    - 标题以中文"微信"开头（排除英文"Weixin"的辅助窗口）
    - 当前可见（IsWindowVisible）
    """
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


def main():
    log("watchdog started; target={}x{}; check={}s".format(
        TARGET_W, TARGET_H, CHECK_INTERVAL))
    miss = 0
    fixes = 0
    while True:
        ws = find_wx_windows()
        if not ws:
            miss += 1
            if miss % 40 == 1:
                log("未找到微信窗口（miss={}）".format(miss))
        for h in ws:
            try:
                r = win32gui.GetWindowRect(h)
                w, ht = r[2] - r[0], r[3] - r[1]
                if ht < 700 or w < 800:
                    win32gui.MoveWindow(h, 0, 0, TARGET_W, TARGET_H, True)
                    fixes += 1
                    log("调整窗口 {}: {}x{} -> {}x{}（第 {} 次）".format(
                        hex(h), w, ht, TARGET_W, TARGET_H, fixes))
                else:
                    miss = 0
            except Exception as e:
                log("窗口操作异常: {}".format(e))
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
