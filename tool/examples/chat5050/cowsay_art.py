#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cowsay 渲染核心 —— 把一段话塞进气泡，配一只 ASCII 小动物。

与网上那些 cowsay 移植版的区别：按「显示宽度」而不是字符个数排版，
中文、全角标点、emoji 都能对齐，所以中文消息也不会把气泡撑歪。

    >>> from cowsay_art import render
    >>> print(render("你好，我是奶牛", face="cow", width=32))
"""

from __future__ import annotations

import random
import unicodedata

# ---------------------------------------------------------------------------
# 形象：{p} 是「气泡指向小动物的那根线」，cowsay 里 say 用 \，think 用 o
# ---------------------------------------------------------------------------

FACES: dict[str, dict[str, str]] = {
    "cow": {
        "label": "奶牛",
        "art": r"""
        {p}   ^__^
         {p}  (oo)\_______
            (__)\       )\/\
                ||----w |
                ||     ||
""",
    },
    "cat": {
        "label": "猫",
        "art": r"""
        {p}   /\_/\
         {p} ( o.o )   ～ meow
            (  > <  )
""",
    },
    "penguin": {
        "label": "企鹅",
        "art": r"""
        {p}   (o_
         {p}  /~\    啪嗒啪嗒
           _| |_
""",
    },
    "ghost": {
        "label": "幽灵",
        "art": r"""
        {p}    .-~~~-.
         {p}  /  o o  \
            \  ^  ^  /   ～呜呜
             '-vvvv-'
""",
    },
    "rabbit": {
        "label": "兔子",
        "art": r"""
        {p}   (\ /)
         {p}  ( . .)   ～啵
            c(")(")
""",
    },
    "dragon": {
        "label": "小龙",
        "art": r"""
        {p}      /^\      /^\
         {p}    <  o  >    <  o  >
            (   ~~   )___(   ~~   )
             \__/\/\__/   \__/\/\__/   嗷～
""",
    },
}

ALIASES = {
    "牛": "cow", "奶牛": "cow", "cow": "cow", "moo": "cow",
    "猫": "cat", "猫咪": "cat", "cat": "cat", "喵": "cat",
    "企鹅": "penguin", "penguin": "penguin", "tux": "penguin",
    "幽灵": "ghost", "鬼": "ghost", "ghost": "ghost",
    "兔子": "rabbit", "兔": "rabbit", "rabbit": "rabbit", "bunny": "rabbit",
    "龙": "dragon", "小龙": "dragon", "dragon": "dragon",
    "随机": "random", "random": "random",
}

DEFAULT_FACE = "cow"


# ---------------------------------------------------------------------------
# 显示宽度
# ---------------------------------------------------------------------------

def char_width(ch: str) -> int:
    """单个字符在等宽字体下占几列。"""
    if unicodedata.combining(ch):
        return 0
    code = ord(ch)
    # 零宽字符
    if code in (0x200B, 0x200C, 0x200D, 0xFEFF):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    # 常见 emoji 区间（没有 Variation Selector 时也算两列）
    if 0x1F300 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF:
        return 2
    return 1


def disp_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


def pad(text: str, width: int) -> str:
    """按显示宽度右侧补空格。"""
    return text + " " * max(0, width - disp_width(text))


# ---------------------------------------------------------------------------
# 换行
# ---------------------------------------------------------------------------

def wrap_display(text: str, width: int) -> list[str]:
    """按显示宽度折行；能落在空格处就落空格，中文则逐字断。"""
    width = max(8, width)
    out: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.replace("\t", "    ").rstrip()
        if not line:
            out.append("")
            continue
        cur = ""
        for ch in line:
            if cur and disp_width(cur) + char_width(ch) > width:
                cut = cur.rfind(" ")
                if cut > 0 and disp_width(cur[:cut]) >= width // 2:
                    out.append(cur[:cut].rstrip())
                    cur = cur[cut + 1:].lstrip()
                else:
                    out.append(cur.rstrip())
                    cur = ""
            cur += ch
        out.append(cur.rstrip())
    return out or [""]


# ---------------------------------------------------------------------------
# 气泡
# ---------------------------------------------------------------------------

def bubble(lines: list[str], think: bool = False) -> str:
    width = max((disp_width(l) for l in lines), default=0)
    # 边框长度 = 正文宽度 + "< " 与 " >" 两个字符，且首列不放空格：
    # 聊天软件通常会对整条消息 strip()，开头有空格会把边框整体推歪。
    top = "_" * (width + 4)
    bottom = "-" * (width + 4)
    body = []

    if len(lines) == 1:
        line = pad(lines[0], width)
        body.append(f"( {line} )" if think else f"< {line} >")
    else:
        for i, line in enumerate(lines):
            cell = pad(line, width)
            if think:
                body.append(f"( {cell} )")
            elif i == 0:
                body.append(f"/ {cell} \\")
            elif i == len(lines) - 1:
                body.append(f"\\ {cell} /")
            else:
                body.append(f"| {cell} |")

    return "\n".join([top, *body, bottom])


def looks_like_art(text: str) -> bool:
    """粗略判断一段文字是不是 ASCII 画（用来避免机器人和机器人互相复读）。"""
    if not text or len(text) < 24:
        return False
    lines = [l for l in text.split("\n") if l.strip()]
    if len(lines) < 4:
        return False
    rulers = marks = 0
    for line in lines:
        body = line.rstrip()
        if set(body) <= set(" _-=~*+"):
            rulers += 1
        elif body.lstrip()[:1] in "\\/|<>()":
            marks += 1
        elif "\\/" in body or "||" in body:
            marks += 1
    return rulers >= 2 and marks >= 2


def resolve_face(name: str | None) -> str:
    if not name:
        return DEFAULT_FACE
    key = name.strip().lower()
    face = ALIASES.get(key) or ALIASES.get(name.strip())
    if face == "random" or key == "random":
        return random.choice(list(FACES))
    return face if face in FACES else DEFAULT_FACE


def render(
    text: str,
    face: str | None = DEFAULT_FACE,
    width: int = 40,
    think: bool = False,
) -> str:
    """把 text 渲染成「气泡 + 小动物」的完整文本。"""
    clean = "".join(
        ch for ch in str(text).replace("\r\n", "\n").replace("\r", "\n")
        if ch == "\n" or unicodedata.category(ch)[0] != "C"
    ).strip()
    if not clean:
        clean = "哞～"

    key = resolve_face(face)
    lines = wrap_display(clean, width)
    art = FACES[key]["art"].format(p="o" if think else "\\")
    return bubble(lines, think=think) + art.rstrip("\n")


def faces_line() -> str:
    return "、".join(f"{k}({v['label']})" for k, v in FACES.items())


if __name__ == "__main__":
    import sys

    msg = " ".join(sys.argv[1:]) or "哞～我想跟你聊天。"
    print(render(msg, width=36))
