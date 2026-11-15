# -*- coding: utf-8 -*-
"""共享桌面 · 浏览器端到端自测（开发自测用）。

用 Chrome DevTools Protocol 真开一个页面，走一遍：
  登录 → 默认线性 → 切换到桌面 → 放便签 → 画一笔 → 缩放 → 他人光标
  → 线性流抽屉 → 切回线性 → 钉到桌面。
同时收集 console.error / 未捕获异常，并截图到 %TEMP%\\desk_shots。

前置：
  1. 服务已启动（默认 http://127.0.0.1:5077，可用 CHAT_BASE 覆盖）
  2. 本机有 Chrome（可用 CHAT_CHROME 覆盖路径）
  3. pip install websocket-client requests

    CHAT_BASE=http://127.0.0.1:5077 py -3.14 tools/desk_ui_check.py

注意：脚本会在测试实例里写入消息与桌面物件，别指向正式数据。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
import websocket

BASE = os.environ.get("CHAT_BASE", "http://127.0.0.1:5077").rstrip("/")
CHROME = os.environ.get(
    "CHAT_CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)
CDP_PORT = int(os.environ.get("CHAT_CDP_PORT", "9223"))
SHOTS = Path(os.environ.get("CHAT_SHOTS", str(Path(tempfile.gettempdir()) / "desk_shots")))
SHOTS.mkdir(parents=True, exist_ok=True)

failures: list[str] = []
errors: list[str] = []

# 本机地址不要走环境里的 HTTP_PROXY（某些环境会把 127.0.0.1 也代理掉）
LOCAL = requests.Session()
LOCAL.trust_env = False


def check(condition, label, detail=""):
    if condition:
        print(f"  ✓ {label}", flush=True)
    else:
        failures.append(label)
        print(f"  ✗ {label}  [{detail}]", flush=True)


class CDP:
    def __init__(self, url: str):
        self.ws = websocket.create_connection(url, timeout=40)
        self.seq = 0
        self.events: list[dict] = []

    def send(self, method: str, params: dict | None = None):
        self.seq += 1
        ident = self.seq
        self.ws.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == ident:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})
            self._collect(message)

    def _collect(self, message: dict):
        method = message.get("method", "")
        params = message.get("params", {})
        if method == "Runtime.exceptionThrown":
            desc = params.get("exceptionDetails", {})
            text = desc.get("text", "exception")
            detail = (desc.get("exception") or {}).get("description", "")
            errors.append(f"exception: {text} {detail}")
        elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            errors.append(
                "console.error: "
                + " ".join(str(a.get("value", a.get("description", ""))) for a in params.get("args", []))
            )
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            url = str(entry.get("url", ""))
            # 首次打开页面时 /api/me 必然返回 401（还没登录），这是预期流程
            expected = (
                entry.get("level") == "error"
                and url.split("?")[0].rstrip("/").endswith("/api/me")
                and "401" in str(entry.get("text", ""))
            )
            if entry.get("level") == "error" and not expected:
                errors.append(f"log: {entry.get('text', '')} {url}")
        self.events.append(message)

    def evaluate(self, expression: str, await_promise: bool = False):
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("text", "evaluate failed"))
        return result.get("result", {}).get("value")

    def wait(self, expression: str, timeout: float = 12.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                last = self.evaluate(expression)
                if last:
                    return True
            except RuntimeError:
                pass
            time.sleep(0.2)
        print(f"    等待超时: {expression} (最后值 {last!r})", flush=True)
        return False

    def shot(self, name: str):
        data = self.send("Page.captureScreenshot", {"format": "png"})["data"]
        path = SHOTS / f"{name}.png"
        path.write_bytes(base64.b64decode(data))
        print(f"    shot: {path}", flush=True)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def main() -> int:
    if not Path(CHROME).is_file():
        print(f"找不到 Chrome: {CHROME}", file=sys.stderr)
        return 2
    try:
        LOCAL.get(f"{BASE}/api/health", timeout=3).raise_for_status()
    except Exception as exc:
        check(False, "服务就绪", f"{BASE} ({exc})")
        return 2

    profile = Path(tempfile.mkdtemp(prefix="chat5050_cdp_"))
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--force-device-scale-factor=1",
            "--window-size=1440,900",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={CDP_PORT}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = None
    try:
        endpoint = f"http://127.0.0.1:{CDP_PORT}"
        target = None
        for _ in range(60):
            try:
                pages = LOCAL.get(f"{endpoint}/json/list", timeout=1).json()
                target = next((p for p in pages if p.get("type") == "page"), None)
                if target:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        if not target:
            check(False, "Chrome DevTools 就绪")
            return 2

        client = CDP(target["webSocketDebuggerUrl"])
        client.send("Page.enable")
        client.send("Runtime.enable")
        client.send("Log.enable")
        client.send("Network.enable")
        dl_dir = SHOTS / "downloads"
        dl_dir.mkdir(parents=True, exist_ok=True)
        for method, params in (
            ("Browser.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": str(dl_dir), "eventsEnabled": True,
            }),
            ("Page.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": str(dl_dir),
            }),
        ):
            try:
                client.send(method, params)
                break
            except RuntimeError:
                continue
        client.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False},
        )
        client.send("Page.navigate", {"url": BASE + "/"})
        check(client.wait("document.readyState === 'complete'"), "页面加载完成")
        check(client.wait("!!window.Chat5050 && !!window.Chat5050Desk"), "桥接层与桌面模块已初始化")

        # ---- 登录 ----
        check(client.wait("!document.getElementById('nickModal').hidden"), "首次弹出昵称框")
        client.evaluate(
            "(() => { const i=document.getElementById('nickInput'); i.value='小明';"
            " document.getElementById('nickForm').requestSubmit(); return true; })()"
        )
        check(
            client.wait("!!(window.Chat5050 && window.Chat5050.state.booted)"),
            "登录并进入大厅",
        )

        # ---- 全局字号功能已移除 ----
        check(
            client.evaluate(
                "!window.Chat5050Font && !document.getElementById('fontSizeBtn')"
                " && !document.getElementById('fontSizePanel')"
                " && !document.querySelector('[data-fs-trigger]')"
            ),
            "全局字号模块 / 按钮 / 面板已移除",
        )
        base_fs = client.evaluate(
            "parseFloat(getComputedStyle(document.querySelector('.brand-text h1')).fontSize)"
        )
        check(bool(base_fs and 12 < base_fs < 30), "界面为原始基础字号（约 17px，已还原 100%）", str(base_fs))

        # ---- 默认线性 ----
        check(client.evaluate("window.Chat5050.getMode() === 'linear'"), "默认线性视图")
        check(
            client.evaluate(
                "!document.getElementById('desk').hidden === false && "
                "document.getElementById('desk').hidden === true"
            ),
            "桌面默认隐藏",
        )
        client.evaluate(
            "fetch('/api/messages', {method:'POST', headers:{'Content-Type':'application/json'},"
            " body: JSON.stringify({text:'这条消息稍后钉到桌面'})}).then(r=>r.json())",
            await_promise=True,
        )
        check(
            client.wait("document.querySelectorAll('#messages article').length > 0"),
            "线性消息已出现",
        )
        client.shot("01-linear")

        # ---- 切换桌面 ----
        client.evaluate("document.getElementById('modeDesk').click()")
        check(client.wait("window.Chat5050.getMode() === 'desk'"), "切换到桌面视图")
        check(
            client.evaluate(
                "!document.getElementById('desk').hidden && "
                "document.querySelector('.app').classList.contains('app--desk')"
            ),
            "桌面容器可见、线性区隐藏",
        )
        size = client.evaluate(
            "(() => { const r=document.getElementById('deskViewport').getBoundingClientRect();"
            " return {w:r.width,h:r.height}; })()"
        )
        check(size and size["w"] > 800 and size["h"] > 400, "画布尺寸正常", str(size))
        client.shot("02-desk-empty")

        # ---- 桌面界面同样使用固定字号 ----
        desk_fs = client.evaluate(
            "parseFloat(getComputedStyle(document.getElementById('deskZoomIn')).fontSize)"
        )
        check(bool(desk_fs and 12 < desk_fs < 30), "桌面控件使用固定字号", str(desk_fs))

        # ---- 桌面附件下载：此前 pointerdown 的 preventDefault 会吞掉 <a download> ----
        upload = LOCAL.post(
            f"{BASE}/api/desk/items",
            params={"nickname": "小明"},
            data={"meta": json.dumps({"kind": "file", "x": 40, "y": 40, "w": 240, "h": 88, "body": "可下载"})},
            files={"file": ("桌面下载说明.txt", b"desk download works", "text/plain")},
            timeout=10,
        )
        check(upload.status_code in (200, 201), "桌面可创建文件物件", str(upload.status_code))
        check(
            client.wait("!!document.querySelector('.desk-file-link')", 15),
            "桌面出现文件卡片与下载入口",
        )
        prevented = client.evaluate(
            """(() => {
              const link = document.querySelector('.desk-file-link');
              if (!link) return 'no-link';
              window.__deskPd = null;
              document.addEventListener('pointerdown',
                (e) => { window.__deskPd = e.defaultPrevented; }, {once: true});
              const r = link.getBoundingClientRect();
              link.dispatchEvent(new PointerEvent('pointerdown', {
                bubbles: true, cancelable: true, pointerId: 991, pointerType: 'mouse',
                button: 0, buttons: 1, clientX: r.left + 4, clientY: r.top + 4
              }));
              return window.__deskPd;
            })()"""
        )
        check(prevented is False, "下载链接的 pointerdown 未被阻止", str(prevented))
        link_info = client.evaluate(
            """(() => {
              const link = document.querySelector('.desk-file-link');
              const r = link.getBoundingClientRect();
              const x = r.left + r.width / 2, y = r.top + r.height / 2;
              return {href: link.getAttribute('href'), name: link.getAttribute('download'),
                      x, y, hit: document.elementFromPoint(x, y) === link};
            })()"""
        )
        check(
            bool(link_info and "download=1" in (link_info.get("href") or "")),
            "下载链接指向附件下载接口",
            str(link_info),
        )
        backend = client.evaluate(
            "fetch(document.querySelector('.desk-file-link').href)"
            ".then(r => ({s: r.status, cd: r.headers.get('content-disposition') || ''}))",
            await_promise=True,
        )
        check(
            bool(backend and backend.get("s") == 200 and "attachment" in backend.get("cd", "")),
            "附件接口返回可下载响应",
            str(backend),
        )
        if link_info and link_info.get("hit"):
            for event_type in ("mousePressed", "mouseReleased"):
                client.send("Input.dispatchMouseEvent", {
                    "type": event_type, "x": link_info["x"], "y": link_info["y"],
                    "button": "left", "clickCount": 1,
                })
        else:
            client.evaluate("document.querySelector('.desk-file-link').click()")
        time.sleep(1.2)
        client.evaluate("1")  # 冲刷 CDP 事件队列
        dl_events = [
            m.get("method") for m in client.events
            if m.get("method") in ("Browser.downloadWillBegin", "Browser.downloadProgress", "Page.downloadWillBegin")
        ]
        net_dl = [
            m["params"]["request"]["url"] for m in client.events
            if m.get("method") == "Network.requestWillBeSent"
            and "download=1" in m.get("params", {}).get("request", {}).get("url", "")
        ]
        files = [p.name for p in dl_dir.iterdir() if p.is_file()]
        check(
            bool(dl_events or net_dl or files),
            "点击后真的触发附件下载",
            f"events={dl_events} net={net_dl[:2]} files={files}",
        )

        # ---- 远端用户新建 / 删除：验证增量同步真的落到 DOM ----
        remote = client.evaluate(
            """fetch('/api/desk/items', {method:'POST',
                headers:{'Content-Type':'application/json'},
                body: JSON.stringify({nickname:'小红', kind:'text', x:-320, y:-160,
                  body:'来自另一个人的便签'})}).then(r=>r.json())""",
            await_promise=True,
        )
        remote_id = (remote or {}).get("item", {}).get("id")
        check(
            client.wait(
                "[...document.querySelectorAll('.desk-note-text')]"
                ".some(n=>n.textContent.includes('来自另一个人的便签'))",
                15,
            ),
            "远端新建的便签实时出现在画布",
        )
        client.evaluate(
            f"fetch('/api/desk/items/{remote_id}?nickname=' + encodeURIComponent('小红'),"
            " {method:'DELETE'}).then(r=>r.json())",
            await_promise=True,
        )
        check(
            client.wait(
                "![...document.querySelectorAll('.desk-note-text')]"
                ".some(n=>n.textContent.includes('来自另一个人的便签'))",
                15,
            ),
            "远端删除实时同步（墓碑生效）",
        )

        # ---- 双击空白 → 便签 ----
        client.evaluate(
            "(() => { const v=document.getElementById('deskViewport');"
            " const r=v.getBoundingClientRect(); const o={bubbles:true,cancelable:true,"
            " clientX:r.left+r.width*0.45, clientY:r.top+r.height*0.42};"
            " v.dispatchEvent(new MouseEvent('dblclick',o)); return true; })()"
        )
        check(
            client.wait("!document.getElementById('deskEditor').hidden"),
            "双击空白打开便签编辑器",
        )
        client.evaluate(
            "(() => { document.getElementById('deskEditorInput').value='桌面第一张便签';"
            " document.getElementById('deskEditorSave').click(); return true; })()"
        )
        check(
            client.wait(
                "(() => { const id = window.Chat5050Desk.selection()[0];"
                " if (!id) return false;"
                " const n = document.querySelector('.desk-item[data-id=\"' + id + '\"] .desk-note-text');"
                " return !!n && n.textContent.includes('桌面第一张便签'); })()",
                15,
            ),
            "便签已放到桌面且自动选中",
        )
        text_ok = client.evaluate(
            "document.querySelector('.desk-item--text .desk-note-text')?.textContent || ''"
        )
        check("桌面第一张便签" in (text_ok or ""), "便签内容正确渲染", str(text_ok))

        # ---- 每张文字便签独立字号 ----
        def server_font(item_id):
            rows = LOCAL.get(
                f"{BASE}/api/desk/snapshot", params={"nickname": "小明"}, timeout=10
            ).json()["items"]
            for row in rows:
                if row["id"] == item_id:
                    return (row.get("style") or {}).get("fontSize")
            return None

        def wait_font(item_id, expected, tries=25):
            for _ in range(tries):
                if server_font(item_id) == expected:
                    return True
                time.sleep(0.2)
            return False

        def note_css(item_id):
            return client.evaluate(
                "(() => { const n=document.querySelector('.desk-item[data-id=\""
                + str(item_id)
                + "\"] .desk-note-text'); return n ? getComputedStyle(n).fontSize : null; })()"
            )

        note_a = client.evaluate("window.Chat5050Desk.selection()[0]")
        check(note_a is not None, "新建便签自动被选中")
        check(
            client.wait("!document.getElementById('deskFontBar').hidden"),
            "选中文字便签出现独立字号条",
        )
        check(
            client.evaluate("document.getElementById('deskFontValue').value") == "18",
            "未设置过的便签默认 18px",
        )
        client.evaluate("document.getElementById('deskFontPlus').click()")
        check(wait_font(note_a, 20.0), "A+ 把这张便签改成 20px 并写进服务端")
        check(note_css(note_a) == "20px", "便签正文按自身字号渲染", str(note_css(note_a)))
        client.evaluate(
            """document.querySelector('#deskFontPresets [data-size="48"]').click()"""
        )
        check(wait_font(note_a, 48.0), "预设「特大」写入服务端")

        cam = client.evaluate("window.Chat5050Desk.camera()")
        resp_b = LOCAL.post(
            f"{BASE}/api/desk/items", params={"nickname": "小明"},
            json={"kind": "text", "x": cam["x"] + 340, "y": cam["y"] + 70,
                  "w": 200, "body": "字号 B"}, timeout=10,
        )
        note_b = resp_b.json().get("item", {}).get("id")
        if not note_b:
            time.sleep(1.5)
            note_b = LOCAL.post(
                f"{BASE}/api/desk/items", params={"nickname": "小明"},
                json={"kind": "text", "x": cam["x"] + 340, "y": cam["y"] + 70,
                      "w": 200, "body": "字号 B"}, timeout=10,
            ).json()["item"]["id"]
        check(
            client.wait(
                "!!document.querySelector('.desk-item[data-id=\"" + str(note_b) + "\"]')", 15
            ),
            "第二张便签同步到画布",
        )
        check(note_css(note_b) == "18px", "另一张便签仍是默认字号", str(note_css(note_b)))
        check(note_css(note_a) == "48px", "第一张保持 48px，没有被带着变", str(note_css(note_a)))

        client.evaluate(f"window.Chat5050Desk.setSelection([{note_a},{note_b}])")
        check(
            client.evaluate("document.getElementById('deskFontScope').textContent") == "2 张",
            "多选时字号条标明作用对象数",
        )
        client.evaluate(
            """document.querySelector('#deskFontPresets [data-size="28"]').click()"""
        )
        check(
            wait_font(note_a, 28.0) and wait_font(note_b, 28.0),
            "多选批量改字号一次请求落库",
        )
        check(
            note_css(note_a) == "28px" and note_css(note_b) == "28px",
            "批量字号在两张便签上各自生效",
            f"{note_css(note_a)} / {note_css(note_b)}",
        )

        client.evaluate("window.Chat5050Desk.setSelection([])")
        client.evaluate(
            "(() => { const v=document.getElementById('deskViewport');"
            " const r=v.getBoundingClientRect(); const o={bubbles:true,cancelable:true,"
            " clientX:r.left+r.width*0.75, clientY:r.top+r.height*0.76};"
            " v.dispatchEvent(new MouseEvent('dblclick',o)); return true; })()"
        )
        check(
            client.wait("!document.getElementById('deskEditor').hidden"),
            "双击空白再次打开便签编辑器",
        )
        editor_font = client.evaluate(
            "(() => { document.getElementById('deskEditorInput').value='独立大字号便签';"
            " const f=document.getElementById('deskEditorFontValue'); f.value='36';"
            " f.dispatchEvent(new Event('change',{bubbles:true}));"
            " return getComputedStyle(document.getElementById('deskEditorInput')).fontSize; })()"
        )
        check(editor_font == "36px", "编辑器按所选字号实时预览", str(editor_font))
        client.evaluate("document.getElementById('deskEditorSave').click()")
        check(
            client.wait("window.Chat5050Desk.selection().length === 1", 15),
            "新便签已创建并选中",
        )
        note_c = client.evaluate("window.Chat5050Desk.selection()[0]")
        check(wait_font(note_c, 36.0), "编辑器选的字号随便签一起保存")
        check(note_css(note_c) == "36px", "新便签按自己的字号渲染", str(note_css(note_c)))
        client.shot("10-note-font")
        client.evaluate("window.Chat5050Desk.setSelection([])")

        # ---- 画一笔 ----
        client.evaluate("document.querySelector('[data-tool=pen]').click()")
        drawn = client.evaluate(
            """(() => {
              const v = document.getElementById('deskViewport');
              const r = v.getBoundingClientRect();
              const mk = (type, x, y) => new PointerEvent(type, {
                bubbles: true, cancelable: true, pointerId: 7, pointerType: 'mouse',
                isPrimary: true, button: 0, buttons: type === 'pointerup' ? 0 : 1,
                clientX: r.left + x, clientY: r.top + y
              });
              v.dispatchEvent(mk('pointerdown', 300, 300));
              for (let i = 1; i <= 14; i++) v.dispatchEvent(mk('pointermove', 300 + i * 9, 300 + i * 4));
              v.dispatchEvent(mk('pointerup', 426, 356));
              return true;
            })()"""
        )
        check(bool(drawn), "画笔指针事件已派发")
        check(
            client.wait("document.querySelectorAll('.desk-item--stroke').length >= 1", 15),
            "笔画已同步到桌面",
        )

        # ---- 缩放 / 平移 ----
        before_zoom = client.evaluate("document.getElementById('deskZoomLabel').textContent")
        client.evaluate("document.getElementById('deskZoomIn').click()")
        after_zoom = client.evaluate("document.getElementById('deskZoomLabel').textContent")
        check(before_zoom != after_zoom, "缩放按钮生效", f"{before_zoom} -> {after_zoom}")
        transform = client.evaluate("document.getElementById('deskWorld').style.transform")
        check("scale(" in (transform or ""), "世界层已应用相机变换", str(transform))

        # ---- 他人光标 ----
        client.evaluate(
            """fetch('/api/desk/cursor', {method:'POST', headers:{'Content-Type':'application/json'},
                 body: JSON.stringify({client_id:'other_tab_99', x:420, y:260, cx:400,
                   cy:250, zoom:1.1, vw:800, vh:600, tool:'pen'})}).then(r=>r.json())""",
            await_promise=True,
        )
        check(client.wait("document.querySelectorAll('.desk-cursor').length >= 1", 15), "看到他人实时光标")
        check(client.wait("document.querySelectorAll('.desk-person').length >= 1", 15), "成员条显示在线者")
        client.evaluate("document.getElementById('deskViewsToggle').click()")
        check(
            client.wait("document.querySelectorAll('.desk-view').length >= 1"),
            "可显示他人视野框",
        )
        client.shot("03-desk-content")

        # ---- 小地图 / 线性流抽屉 ----
        client.evaluate("document.getElementById('deskDrawerToggle').click()")
        check(client.wait("!document.getElementById('deskDrawer').hidden"), "线性流抽屉可打开")
        check(
            client.wait("document.querySelectorAll('#deskDrawerList .desk-drawer-msg').length >= 1"),
            "抽屉里能看到线性消息",
        )
        client.shot("04-desk-drawer")
        client.evaluate("document.getElementById('deskDrawerClose').click()")

        # ---- 切回线性 + 钉到桌面 ----
        client.evaluate("document.getElementById('deskBackBtn').click()")
        check(client.wait("window.Chat5050.getMode() === 'linear'"), "可切回线性视图")
        check(
            client.wait("document.getElementById('desk').hidden === true"),
            "桌面容器重新隐藏",
        )
        check(
            client.evaluate("document.querySelectorAll('#messages [data-act=pin-desk]').length > 0"),
            "线性消息带「钉到桌面」入口",
        )
        client.evaluate("document.querySelector('#messages [data-act=pin-desk]').click()")
        check(client.wait("window.Chat5050.getMode() === 'desk'", 15), "钉到桌面后自动切到桌面")
        check(
            client.wait("document.querySelectorAll('.desk-item--text').length >= 2", 15),
            "线性消息已变成桌面物件",
        )
        client.shot("05-pinned")

        # ---- Ctrl 多选 / 整组拖动 / 批量删除 / 框选 / Ctrl+A ----
        # 前面的「画一笔」把工具切到了 pen，框选/多选前须回到选择工具
        client.evaluate("document.querySelector('[data-tool=select]').click()")
        check(
            client.evaluate("document.getElementById('deskViewport').dataset.tool") == "select",
            "多选前已回到选择工具",
        )

        def make_notes(prefix, count, y_offset=60):
            cam = client.evaluate("window.Chat5050Desk.camera()")
            made = []
            for i in range(count):
                response = LOCAL.post(
                    f"{BASE}/api/desk/items",
                    params={"nickname": "小明"},
                    json={
                        "kind": "text",
                        "x": cam["x"] + (i - (count - 1) / 2) * 230,
                        "y": cam["y"] + y_offset,
                        "w": 180,
                        "body": f"{prefix}{i}",
                    },
                    timeout=10,
                )
                payload = response.json()
                if "item" not in payload:
                    print(
                        f"    create failed: {response.status_code} {payload}",
                        flush=True,
                    )
                    time.sleep(1.5)
                    response = LOCAL.post(
                        f"{BASE}/api/desk/items",
                        params={"nickname": "小明"},
                        json={
                            "kind": "text",
                            "x": cam["x"] + (i - (count - 1) / 2) * 230,
                            "y": cam["y"] + y_offset,
                            "w": 180,
                            "body": f"{prefix}{i}",
                        },
                        timeout=10,
                    )
                    payload = response.json()
                made.append(payload["item"]["id"])
            expr = " && ".join(
                "!!document.querySelector('.desk-item[data-id=\"%s\"]')" % item_id
                for item_id in made
            )
            client.wait(expr, 15)
            return made

        def snapshot():
            return {
                row["id"]: (row["x"], row["y"])
                for row in LOCAL.get(
                    f"{BASE}/api/desk/snapshot", params={"nickname": "小明"}, timeout=10
                ).json()["items"]
            }

        multi = make_notes("多选测试", 3)
        client.evaluate("window.Chat5050Desk.setSelection([])")

        def ctrl_click(item_id, pointer_id):
            expr = """(() => {
              const node = document.querySelector('.desk-item[data-id="%s"]');
              if (!node) return 'no-node';
              const r = node.getBoundingClientRect();
              const base = {bubbles:true, cancelable:true, pointerId:%s, pointerType:'mouse',
                            isPrimary:true, button:0, ctrlKey:true,
                            clientX:r.left+30, clientY:r.top+20};
              const down = new PointerEvent('pointerdown', {...base, buttons:1});
              node.dispatchEvent(down);
              const during = window.Chat5050Desk.selection();
              node.dispatchEvent(new PointerEvent('pointerup', {...base, buttons:0}));
              return {ctrl: down.ctrlKey, target: node.className,
                      during: during, after: window.Chat5050Desk.selection()};
            })()""" % (item_id, pointer_id)
            return client.evaluate(expr)

        step1 = ctrl_click(multi[0], 31)
        step2 = ctrl_click(multi[1], 32)
        check(
            bool(step1 and step2 and isinstance(step1, dict) and step1.get("ctrl")),
            "Ctrl 点击事件已派发",
        )
        selection = client.evaluate("window.Chat5050Desk.selection()")
        check(
            set(multi[:2]).issubset(set(selection or [])) and len(selection) == 2,
            "Ctrl + 点击选中了 2 个物件",
            str(selection),
        )
        check(
            "已选" in (client.evaluate("document.getElementById('deskHintText').textContent") or ""),
            "HUD 提示当前多选数量",
        )

        before = snapshot()
        drag_expr = """(() => {
          const node = document.querySelector('.desk-item[data-id="%s"]');
          const r = node.getBoundingClientRect();
          const v = document.getElementById('deskViewport');
          const x = r.left+30, y = r.top+20;
          const mk = (t,cx,cy,b) => new PointerEvent(t, {bubbles:true,cancelable:true,
            pointerId:32, pointerType:'mouse', isPrimary:true, button:0, buttons:b,
            clientX:cx, clientY:cy});
          node.dispatchEvent(mk('pointerdown',x,y,1));
          for (let i=1;i<=6;i++) v.dispatchEvent(mk('pointermove',x+i*8,y+i*5,1));
          v.dispatchEvent(mk('pointerup',x+48,y+30,0));
          return true;
        })()""" % multi[0]
        check(bool(client.evaluate(drag_expr)), "整组拖动事件已派发")
        time.sleep(1.0)
        after = snapshot()
        check(
            all(
                i in after and i in before and after[i] != before[i]
                for i in multi[:2]
            ),
            "整组拖动通过批量接口同步到服务端",
            f"{[(before.get(i), after.get(i)) for i in multi[:2]]}",
        )

        client.evaluate(
            "document.dispatchEvent(new KeyboardEvent('keydown',"
            "{key:'Delete',bubbles:true,cancelable:true}))"
        )
        time.sleep(0.9)
        remaining = snapshot()
        check(
            all(i not in remaining for i in multi[:2]) and multi[2] in remaining,
            "Delete 批量删除选中物件，未选中的保留",
            str(sorted(remaining)[-6:]),
        )

        frame = make_notes("框选测试", 3, y_offset=-160)
        client.evaluate("document.querySelector('[data-tool=marquee]').click()")
        check(
            client.evaluate("document.getElementById('deskViewport').dataset.tool") == "marquee",
            "可切换到框选工具",
        )
        box = client.evaluate(
            """(() => {
              const ids = [%s];
              let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
              for (const id of ids) {
                const r = document.querySelector('.desk-item[data-id="' + id + '"]').getBoundingClientRect();
                x0=Math.min(x0,r.left); y0=Math.min(y0,r.top);
                x1=Math.max(x1,r.right); y1=Math.max(y1,r.bottom);
              }
              return {x0:x0-18,y0:y0-18,x1:x1+18,y1:y1+18};
            })()""" % ", ".join(str(i) for i in frame[:2])
        )
        marquee_expr = """(() => {
          const v = document.getElementById('deskViewport');
          const mk = (t,cx,cy,b) => new PointerEvent(t, {bubbles:true,cancelable:true,
            pointerId:33, pointerType:'mouse', isPrimary:true, button:0, buttons:b,
            clientX:cx, clientY:cy});
          v.dispatchEvent(mk('pointerdown',%s,%s,1));
          v.dispatchEvent(mk('pointermove',%s,%s,1));
          v.dispatchEvent(mk('pointerup',%s,%s,0));
          return true;
        })()""" % (box["x0"], box["y0"], box["x1"], box["y1"], box["x1"], box["y1"])
        check(bool(client.evaluate(marquee_expr)), "拖出框选矩形")
        selected_by_box = client.evaluate("window.Chat5050Desk.selection()")
        check(
            set(frame[:2]).issubset(set(selected_by_box or [])),
            "框选命中矩形内的物件",
            str(selected_by_box),
        )
        client.shot("09-multi-select")
        client.evaluate(
            "document.dispatchEvent(new KeyboardEvent('keydown',"
            "{key:'a',ctrlKey:true,bubbles:true,cancelable:true}))"
        )
        all_selected = client.evaluate("window.Chat5050Desk.selection()")
        check(
            len(all_selected or []) == client.evaluate("window.Chat5050Desk.itemCount"),
            "Ctrl + A 全选所有物件",
            f"{len(all_selected or [])} / {client.evaluate('window.Chat5050Desk.itemCount')}",
        )
        client.evaluate(
            "document.dispatchEvent(new KeyboardEvent('keydown',"
            "{key:'Escape',bubbles:true,cancelable:true}))"
        )
        check(
            not client.evaluate("window.Chat5050Desk.selection()"),
            "Escape 清空选择",
        )
        client.evaluate("document.querySelector('[data-tool=select]').click()")

        # ---- 窄屏：不横向溢出 ----
        client.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
        )
        time.sleep(0.5)
        overflow = client.evaluate(
            "({sw: document.documentElement.scrollWidth, iw: window.innerWidth})"
        )
        check(
            overflow and overflow["sw"] <= overflow["iw"] + 1,
            "390px 窄屏无横向溢出",
            str(overflow),
        )
        toolbar = client.evaluate(
            "(() => { const r=document.getElementById('deskTools').getBoundingClientRect();"
            " return {left:r.left, right:r.right, w:r.width}; })()"
        )
        check(
            toolbar and toolbar["left"] >= 0 and toolbar["right"] <= 391,
            "窄屏工具条仍在可视区内",
            str(toolbar),
        )
        client.shot("06-desk-mobile")

        # ---- 刷新后仍无全局字号入口，且便签各自字号都保留 ----
        client.send("Page.reload", {"ignoreCache": True})
        time.sleep(1.2)
        check(
            client.wait(
                "document.readyState === 'complete' && !!window.Chat5050 && !window.Chat5050Font",
                20,
            ),
            "刷新后页面重新加载且没有全局字号模块",
        )
        check(
            server_font(note_a) == 28.0 and server_font(note_c) == 36.0,
            "刷新后每张便签仍保留各自独立的字号",
            f"{server_font(note_a)} / {server_font(note_c)}",
        )

        # ---- 无 JS 报错 ----
        time.sleep(0.6)
        check(not errors, "无 console.error / 未捕获异常", "; ".join(errors[:4]))
        return 1 if failures else 0
    finally:
        if client:
            client.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    code = main()
    print("\n" + "=" * 58)
    if failures:
        print(f"失败 {len(failures)} 项：")
        for item in failures:
            print("  -", item)
    else:
        print("全部通过 · 共享桌面浏览器自测")
    print("=" * 58)
    sys.exit(code)
