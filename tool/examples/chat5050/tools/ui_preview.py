# -*- coding: utf-8 -*-
"""用 Playwright 走一遍 Web 端权限 / 撤回流程并截图（开发自测用）。

前置：
    python app.py（默认 5050）。可用 CHAT_BASE 指定地址，CHAT_CHROME 指定浏览器。
注意：
    脚本会真的禁言 / 锁定示例昵称并撤回消息，请指向测试实例（默认 5051），
    不要直接对正式数据运行。
产物：
    %TEMP%\\ui_shots\\*.png
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("CHAT_BASE", "http://127.0.0.1:5051")
SHOTS = Path(os.environ.get("CHAT_SHOTS", str(Path(os.environ["TEMP"]) / "ui_shots")))
CHROME = os.environ.get(
    "CHAT_CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)
NO_ANIM = (
    "*,*::before,*::after{animation:none!important;transition:none!important;"
    "scroll-behavior:auto!important}"
)

SHOTS.mkdir(parents=True, exist_ok=True)


def shot(page, name: str) -> None:
    path = SHOTS / f"{name}.png"
    page.screenshot(path=str(path), timeout=25000, animations="disabled")
    print("shot:", path, flush=True)


def login(page, nickname: str) -> None:
    page.goto(BASE, wait_until="domcontentloaded")
    page.add_style_tag(content=NO_ANIM)
    page.wait_for_selector("#nickModal:not([hidden])", timeout=20000)
    page.fill("#nickInput", nickname)
    page.click("#nickSubmit", force=True, timeout=15000)
    page.wait_for_selector("#messages article", timeout=20000)
    page.wait_for_timeout(900)


def main() -> int:
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=CHROME,
            headless=True,
            args=["--disable-gpu", "--disable-lcd-text", "--force-device-scale-factor=1"],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 880})
        page = context.new_page()
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.on(
            "console",
            lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error"
            else None,
        )

        login(page, "root")
        print("admin button visible:", page.is_visible("#adminBtn"), flush=True)
        print("me role visible:", page.is_visible("#meRole"), flush=True)
        shot(page, "01-root-chat")

        # 管理员面板
        page.click("#adminBtn", force=True)
        page.wait_for_selector("#adminList .admin-person", timeout=15000)
        page.wait_for_timeout(500)
        shot(page, "02-admin-panel")

        # 搜索 + 禁言 10 分钟
        page.fill("#adminSearch", "牛哞哞")
        page.wait_for_timeout(400)
        row = page.locator("#adminList .admin-person").first
        print("filtered row:", row.locator(".admin-person-name").inner_text(), flush=True)
        row.locator("select[data-mute]").select_option("600")
        page.wait_for_timeout(1200)
        print("mute badge:", "禁言中" in page.inner_text("#adminList"), flush=True)
        shot(page, "03-admin-muted")

        # 解除禁言
        page.fill("#adminSearch", "牛哞哞")
        page.wait_for_timeout(300)
        page.locator("#adminList .admin-person").first.locator(
            "select[data-mute]"
        ).select_option("off")
        page.wait_for_timeout(1200)

        # 锁定昵称
        page.on("dialog", lambda dialog: dialog.accept())
        page.locator("#adminList .admin-person").first.locator(
            'button[data-act="lock"]'
        ).click(force=True)
        page.wait_for_timeout(1400)
        print("locked badge:", "已锁定" in page.inner_text("#adminList"), flush=True)
        shot(page, "04-admin-locked")

        # 关闭面板，撤回一条消息
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        target = page.locator("#messages article").first
        target.hover()
        target.locator('button[data-act="recall"]').first.click(force=True)
        page.wait_for_timeout(1200)
        print(
            "recalled placeholder:",
            "该消息已被撤回" in page.inner_text("#messages"),
            flush=True,
        )
        shot(page, "05-recalled")

        # 撤销撤回
        target = page.locator("#messages article").first
        target.hover()
        target.locator('button[data-act="unrecall"]').first.click(force=True)
        page.wait_for_timeout(1200)
        print(
            "recall undone:",
            "该消息已被撤回" not in page.inner_text("#messages"),
            flush=True,
        )
        shot(page, "06-unrecalled")

        # 第二个用户：验证禁言横幅 / 普通用户视角
        ctx2 = browser.new_context(viewport={"width": 430, "height": 800})
        page2 = ctx2.new_page()
        page2.on("pageerror", lambda exc: errors.append(f"p2 pageerror: {exc}"))
        page2.on(
            "console",
            lambda msg: errors.append(f"p2 console.{msg.type}: {msg.text}")
            if msg.type == "error"
            else None,
        )
        login(page2, "牛哞哞")
        page2.wait_for_timeout(500)
        print("admin btn hidden for normal user:", not page2.is_visible("#adminBtn"), flush=True)

        page.click("#adminBtn", force=True)
        page.wait_for_selector("#adminList .admin-person", timeout=15000)
        page.fill("#adminSearch", "牛哞哞")
        page.wait_for_timeout(400)
        page.locator("#adminList .admin-person").first.locator(
            "select[data-mute]"
        ).select_option("3600")
        page.wait_for_timeout(1200)
        page.keyboard.press("Escape")

        page2.wait_for_timeout(3500)
        print("mute notice visible:", page2.is_visible("#muteNotice"), flush=True)
        print("input disabled:", page2.is_disabled("#input"), flush=True)
        print(
            "mute text:",
            page2.inner_text("#muteNoticeText") if page2.is_visible("#muteNotice") else "-",
            flush=True,
        )
        shot(page2, "07-muted-mobile")

        browser.close()

    if errors:
        print("\nJS 错误：", flush=True)
        for err in errors:
            print(" -", err, flush=True)
        return 1
    print("\nUI 检查完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
