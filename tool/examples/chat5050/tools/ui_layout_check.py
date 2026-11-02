# -*- coding: utf-8 -*-
"""布局 / 交互断言（不依赖截图，纯 DOM 尺寸检查）。"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("CHAT_BASE", "http://127.0.0.1:5051")
CHROME = os.environ.get(
    "CHAT_CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)
NO_ANIM = (
    "*,*::before,*::after{animation:none!important;transition:none!important;"
    "scroll-behavior:auto!important}"
)

failures = []


def check(condition, label, detail=""):
    if condition:
        print(f"  ✓ {label}", flush=True)
    else:
        failures.append(label)
        print(f"  ✗ {label}  [{detail}]", flush=True)


def login(page, nickname):
    page.goto(BASE, wait_until="domcontentloaded")
    page.add_style_tag(content=NO_ANIM)
    page.wait_for_selector("#nickModal:not([hidden])", timeout=20000)
    page.fill("#nickInput", nickname)
    page.click("#nickSubmit", force=True, timeout=15000)
    page.wait_for_selector("#messages article", timeout=20000)
    page.wait_for_timeout(700)


def no_overflow(page):
    return page.evaluate(
        "() => ({sw: document.documentElement.scrollWidth, iw: innerWidth,"
        " body: document.body.scrollWidth})"
    )


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=CHROME,
            headless=True,
            args=["--disable-gpu", "--disable-lcd-text", "--force-device-scale-factor=1"],
        )

        # ---- 桌面端 ----
        ctx = browser.new_context(viewport={"width": 1280, "height": 880})
        page = ctx.new_page()
        login(page, "root")
        sizes = no_overflow(page)
        check(sizes["sw"] <= sizes["iw"], "桌面端没有横向溢出", str(sizes))

        check(page.is_visible("#adminBtn"), "管理员能看到「管理」入口")
        check(page.is_visible("#meRole"), "管理员标识可见")

        # 悬停出现撤回按钮
        first = page.locator("#messages article").first
        first.hover()
        page.wait_for_timeout(150)
        opacity = first.locator(".msg-actions").evaluate(
            "el => getComputedStyle(el).opacity"
        )
        check(float(opacity) > 0.5, "悬停后撤回按钮出现", f"opacity={opacity}")

        # 管理面板
        page.click("#adminBtn", force=True)
        page.wait_for_selector("#adminList .admin-person", timeout=15000)
        page.wait_for_timeout(300)
        card = page.locator(".admin-card").bounding_box()
        check(card["width"] <= 760 + 1, "管理面板宽度不超过 760px", str(card["width"]))
        check(card["x"] >= 0 and card["x"] + card["width"] <= 1280, "管理面板在视口内")
        list_box = page.locator("#adminList").bounding_box()
        check(list_box["height"] <= 500, "名单区域限制高度并可滚动", str(list_box["height"]))
        row_count = page.locator("#adminList .admin-person").count()
        check(row_count >= 10, f"名单加载了 {row_count} 个昵称")
        # 行内按钮不溢出
        over = page.evaluate(
            """() => {
              const rows = [...document.querySelectorAll('.admin-person')];
              return rows.filter(r => {
                const rr = r.getBoundingClientRect();
                return [...r.querySelectorAll('button,select')].some(b => {
                  const bb = b.getBoundingClientRect();
                  return bb.right > rr.right + 1 || bb.left < rr.left - 1;
                });
              }).length;
            }"""
        )
        check(over == 0, "管理行内按钮没有溢出", f"{over} 行溢出")
        stats = page.inner_text("#adminStats")
        check("管理员" in stats and "在线" in stats, "统计条包含管理员 / 在线", stats)

        # 移动端布局（同一管理员，窄屏打开面板）
        page.keyboard.press("Escape")
        page.set_viewport_size({"width": 390, "height": 780})
        page.wait_for_timeout(300)
        sizes = no_overflow(page)
        check(sizes["sw"] <= sizes["iw"], "窄屏聊天页没有横向溢出", str(sizes))
        page.click("#adminBtn", force=True)
        page.wait_for_selector("#adminList .admin-person", timeout=15000)
        page.wait_for_timeout(300)
        card = page.locator(".admin-card").bounding_box()
        check(card["x"] >= 0 and card["x"] + card["width"] <= 391, "窄屏管理面板不溢出", str(card))
        page.keyboard.press("Escape")

        # ---- 普通用户（被禁言） ----
        ctx2 = browser.new_context(viewport={"width": 430, "height": 800})
        p2 = ctx2.new_page()
        login(p2, "牛哞哞")
        sizes = no_overflow(p2)
        check(sizes["sw"] <= sizes["iw"], "移动端没有横向溢出", str(sizes))
        check(not p2.is_visible("#adminBtn"), "普通用户看不到管理入口")
        check(p2.is_visible("#muteNotice"), "被禁言用户看到禁言横幅")
        check(p2.is_disabled("#input"), "被禁言用户输入框禁用")
        check(p2.is_disabled("#sendBtn"), "被禁言用户发送按钮禁用")
        banner = p2.locator("#muteNotice").bounding_box()
        check(banner["width"] <= 430, "禁言横幅不溢出", str(banner["width"]))
        text = p2.inner_text("#muteNoticeText")
        check("禁言" in text, "禁言横幅显示原因", text)

        # 普通用户自己的消息有撤回按钮（若有自己的消息）
        p2.evaluate("() => window.scrollTo(0, 0)")
        browser.close()

    if failures:
        print("\n失败项：" + "；".join(failures), flush=True)
        return 1
    print("\n布局检查全部通过", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
