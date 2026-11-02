# -*- coding: utf-8 -*-
"""微信消息桥接：轮询 wechat_headless_api 服务的新消息并输出（供上层通知消费）。

- 只读 HTTP API（127.0.0.1:8900），不直接操作微信窗口，避免窗口竞争
- 每条新消息输出一行 JSON（stdout，flush）+ 追加写入 logs/bridge_log.jsonl
- 保持系统唤醒（进程级；进程退出即自动恢复，不修改任何系统设置）
- 启动时自动等待 API 服务就绪
- 服务持续失败时输出 error 行（触发上层告警），自动重试
"""
import ctypes
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "bridge_log.jsonl")


def load_cfg():
    cfg = {"host": "127.0.0.1", "port": 8900, "default_chat": "文件传输助手"}
    p = os.path.join(BASE, "..", "wechat_headless_api", "config.json")
    try:
        with open(p, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


CFG = load_cfg()
API = "http://{}:{}".format(CFG.get("host", "127.0.0.1"), CFG.get("port", 8900))
CHATS = CFG.get("chats") or [CFG.get("default_chat") or "文件传输助手"]
POLL = 2.5  # 秒

# ---- 保持系统唤醒（进程级，不修改系统设置）----
try:
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002
    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
except Exception:
    pass

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_last_n = [0]


def next_n():
    n = int(time.time() * 1000)
    if n <= _last_n[0]:
        n = _last_n[0] + 1
    _last_n[0] = n
    return n


def emit(obj):
    line = json.dumps(obj, ensure_ascii=False)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def http_json(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    # 1) 等待 API 服务就绪
    for _ in range(150):  # 最多等待 5 分钟
        try:
            h = http_json(API + "/api/health")
            if h.get("ok"):
                emit({"n": next_n(), "ts": time.time(), "source": "bridge",
                      "level": "info", "message": "bridge started, service ready",
                      "my_info": h.get("my_info"), "chats": CHATS})
                break
        except Exception:
            pass
        time.sleep(2)
    else:
        emit({"n": next_n(), "ts": time.time(), "source": "bridge", "level": "error",
              "message": "服务未就绪（等待超时），仍进入轮询"})

    # 2) 轮询新消息
    fail_streak = 0
    last_warn = 0.0
    while True:
        try:
            for chat in CHATS:
                url = "{}/api/messages?chat={}&drain=1".format(
                    API, urllib.parse.quote(chat))
                d = http_json(url)
                for m in (d.get("messages") or []):
                    emit({"n": next_n(), "ts": time.time(), "source": "wechat", **m})
            fail_streak = 0
        except Exception as e:
            fail_streak += 1
            now = time.time()
            if fail_streak >= 30 and (now - last_warn) > 240:
                emit({"n": next_n(), "ts": now, "source": "bridge", "level": "error",
                      "message": "API 持续失败: {}".format(e), "fail_streak": fail_streak})
                last_warn = now
        time.sleep(POLL)


if __name__ == "__main__":
    main()
