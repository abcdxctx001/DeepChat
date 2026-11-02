# -*- coding: utf-8 -*-
"""经 HTTP 服务发送微信消息（推荐日常使用；走 serve 进程，不直接操作微信窗口）。

用法：
  python wx_send.py --file msg.txt [--chat 文件传输助手]
  python wx_send.py --text "内容" [--chat 文件传输助手]
  python wx_send.py --attach "C:\\path\\a.png[;C:\\path\\b.png...]" [--chat 文件传输助手]
  type msg.txt | python wx_send.py
"""
import argparse
import io
import json
import os
import sys
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def load_cfg():
    cfg = {"host": "127.0.0.1", "port": 8900, "default_chat": "文件传输助手"}
    p = os.path.join(BASE, "..", "wechat_headless_api", "config.json")
    try:
        with open(p, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text")
    ap.add_argument("--file")
    ap.add_argument("--attach", help="发送文件（多个用英文分号分隔）")
    ap.add_argument("--chat")
    a = ap.parse_args()

    cfg = load_cfg()
    chat = a.chat or cfg.get("default_chat") or "文件传输助手"
    host = cfg.get("host", "127.0.0.1")
    port = cfg.get("port", 8900)

    # --- 发送文件模式 ---
    if a.attach:
        files = [p.strip() for p in a.attach.split(";") if p.strip()]
        if not files:
            print("! 文件列表为空")
            return 2
        payload = json.dumps({"chat": chat, "files": files}, ensure_ascii=False).encode("utf-8")
        url = "http://{}:{}/api/sendfile".format(host, port)
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                print(r.read().decode("utf-8"))
            print("* sent {} file(s) -> {}".format(len(files), chat))
            return 0
        except Exception as e:
            print("发送失败:", e)
            return 1

    if a.file:
        with open(a.file, encoding="utf-8") as f:
            text = f.read()
    elif a.text is not None:
        text = a.text
    else:
        text = sys.stdin.read()
    text = (text or "").rstrip("\n")
    if not text.strip():
        print("! 内容为空，未发送")
        return 2

    payload = json.dumps({"chat": chat, "text": text}, ensure_ascii=False).encode("utf-8")
    url = "http://{}:{}/api/send".format(host, port)
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            print(r.read().decode("utf-8"))
        print("* sent {} chars -> {}".format(len(text), chat))
        return 0
    except Exception as e:
        print("发送失败:", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
