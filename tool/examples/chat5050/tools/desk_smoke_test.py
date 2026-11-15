# -*- coding: utf-8 -*-
"""共享桌面 · API 端到端测试（不污染 data/）。

自行用临时数据目录拉起一个真实 app.py 进程，覆盖：
  数据模型 / 快照与增量 / 长轮询唤醒 / 权限与禁言 / 笔画校验 /
  文件上传 / 光标在线 / 软删除与恢复 / 线性流回归 / XSS 文本安全。

    py -3.14 tools/desk_smoke_test.py --port 5078
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
passed = 0
failed: list[str] = []


def check(condition, label, detail=""):
    global passed
    if condition:
        passed += 1
        print(f"  ✓ {label}", flush=True)
    else:
        failed.append(label)
        print(f"  ✗ {label}  [{detail}]", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5078)
    args = parser.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    data_dir = Path(tempfile.mkdtemp(prefix="chat5050_desk_api_"))
    env = dict(os.environ)
    env.update(
        CHAT_DATA_DIR=str(data_dir),
        CHAT_PORT=str(args.port),
        CHAT_HOST="127.0.0.1",
    )
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "app.py")],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    session = requests.Session()
    session.trust_env = False  # 本机测试不走环境代理
    try:
        for _ in range(80):
            try:
                if session.get(f"{base}/api/health", timeout=1).ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.15)
        else:
            print("服务启动超时", file=sys.stderr)
            return 2

        def url(path):
            return base + path

        def j(res):
            try:
                return res.json()
            except ValueError:
                return {}

        # ------------------------------------------------------------------
        print("\n[1] 健康检查 / 群信息 / 线性 API 回归")
        health = j(session.get(url("/api/health")))
        check(health.get("version") == "2.1.0", "版本号升级到 2.1.0", health.get("version"))
        check("3" in health.get("schema", []), "schema_version 包含 3")
        check(health.get("desk_item_count") == 0, "健康检查带桌面物件数")
        group = j(session.get(url("/api/group")))["group"]
        check("desk_item_count" in group and "desk_active_count" in group, "群信息暴露桌面统计")

        linear = session.post(url("/api/messages?nickname=alice"), json={"text": "线性回归消息"})
        check(linear.status_code == 201, "原有发送文字接口仍可用", linear.status_code)
        msg_list = j(session.get(url("/api/messages?nickname=alice")))
        check(any(m["text"] == "线性回归消息" for m in msg_list["messages"]), "线性消息可读")

        # ------------------------------------------------------------------
        print("\n[2] 放置文字便签 / 快照 / 增量 / XSS 安全")
        xss = '<script>alert(1)</script> & "引号"'
        created = session.post(
            url("/api/desk/items"),
            json={
                "nickname": "alice",
                "kind": "text",
                "x": 120.5,
                "y": -80,
                "body": xss,
                "style": {"note": "#ffcc00", "color": "url(javascript:alert(1))"},
            },
        )
        check(created.status_code == 201, "创建文字便签", created.status_code)
        item = j(created).get("item", {})
        item_id = item.get("id")
        check(item.get("body") == xss, "用户文本原样保存（由前端 textContent 渲染）", item.get("body"))
        check(item.get("style", {}).get("note") == "#ffcc00", "合法颜色被保留")
        check("color" not in item.get("style", {}), "非法颜色被过滤")
        check(item.get("w") == 260 and item.get("h") == 0, "便签默认宽度 / 高度自适应")
        check(item.get("rev", 0) >= 1, "物件带版本号")

        snap = j(session.get(url("/api/desk/snapshot?nickname=bob")))
        check(len(snap.get("items", [])) == 1, "快照返回 1 个物件", snap.get("count"))
        check(snap.get("has_more") is False, "快照不分页")
        rev = snap.get("rev", 0)
        prev = snap.get("presence_rev", 0)

        changes = j(session.get(url(f"/api/desk/events?nickname=bob&since_rev={rev - 1}")))
        check(len(changes.get("items", [])) == 1, "增量能按 rev 拉取变化")
        empty = j(session.get(url(f"/api/desk/events?nickname=bob&since_rev={rev}")))
        check(empty.get("items") == [], "无变化时不重复下发")

        # ------------------------------------------------------------------
        print("\n[3] 长轮询：新物件到达时立即唤醒")
        holder = {}

        def long_poll():
            started = time.monotonic()
            res = session.get(
                url(
                    f"/api/desk/events?nickname=bob&since_rev={rev}"
                    f"&since_presence={prev}&wait=10"
                ),
                timeout=15,
            )
            holder["elapsed"] = time.monotonic() - started
            holder["data"] = j(res)

        thread = threading.Thread(target=long_poll, daemon=True)
        thread.start()
        time.sleep(0.6)
        session.post(
            url("/api/desk/items"),
            json={"nickname": "bob", "kind": "text", "x": 900, "y": 900, "body": "唤醒测试"},
        )
        thread.join(timeout=12)
        check(not thread.is_alive(), "长轮询已返回")
        check(holder.get("elapsed", 99) < 4, "新物件立即唤醒（<4s）", holder.get("elapsed"))
        check(
            any(i.get("body") == "唤醒测试" for i in holder.get("data", {}).get("items", [])),
            "唤醒响应里带着新物件",
        )
        rev = max(rev, holder.get("data", {}).get("rev", rev))

        # ------------------------------------------------------------------
        print("\n[4] 移动 / 编辑 / 层级：权限边界")
        moved = session.patch(
            url(f"/api/desk/items/{item_id}?nickname=bob"), json={"x": 333}
        )
        check(moved.status_code == 200, "任何人可移动他人物件（共享桌面）", moved.status_code)
        check(j(moved)["item"]["x"] == 333, "移动后坐标生效")
        check(j(moved)["item"]["updated_by"] == "bob", "记录最后移动者")

        denied = session.patch(
            url(f"/api/desk/items/{item_id}?nickname=bob"), json={"body": "篡改"}
        )
        check(denied.status_code == 403, "他人不能改便签内容", denied.status_code)
        check(j(denied)["error"]["code"] == "forbidden", "返回 forbidden 错误码")

        edited = session.patch(
            url(f"/api/desk/items/{item_id}?nickname=alice"), json={"body": "我改的"}
        )
        check(j(edited).get("item", {}).get("body") == "我改的", "作者可以编辑内容")

        clamped = session.post(
            url("/api/desk/items"),
            json={"nickname": "alice", "kind": "text", "x": 1e12, "y": -1e12, "body": "越界"},
        )
        check(abs(j(clamped)["item"]["x"]) <= 5_000_000, "超大坐标被夹到上限")

        # ------------------------------------------------------------------
        print("\n[5] 软删除 / 恢复 / 管理员")
        bad_delete = session.delete(url(f"/api/desk/items/{item_id}?nickname=bob"))
        check(bad_delete.status_code == 403, "非作者不能删除", bad_delete.status_code)
        deleted = session.delete(url(f"/api/desk/items/{item_id}?nickname=alice"))
        check(deleted.status_code == 200 and j(deleted)["item"]["deleted"], "作者删除成功")
        snap_after_delete = j(session.get(url("/api/desk/snapshot?nickname=alice")))["items"]
        check(
            not any(i["id"] == item_id for i in snap_after_delete),
            "快照不再包含已删除物件",
            [i["id"] for i in snap_after_delete],
        )
        tombstone = j(session.get(url(f"/api/desk/events?nickname=alice&since_rev={rev - 1}")))
        check(
            any(i.get("id") == item_id and i.get("deleted") for i in tombstone["items"]),
            "增量同步会下发删除墓碑",
        )
        restored = session.post(
            url(f"/api/desk/items/{item_id}/restore?nickname=alice")
        )
        check(restored.status_code == 200, "可撤销删除", restored.status_code)
        snap_after_restore = j(session.get(url("/api/desk/snapshot?nickname=alice")))["items"]
        check(
            any(i["id"] == item_id for i in snap_after_restore),
            "恢复后重新出现在快照",
            [i["id"] for i in snap_after_restore],
        )

        admin_item = session.post(
            url("/api/desk/items"),
            json={"nickname": "bob", "kind": "text", "x": 0, "y": 0, "body": "bob 的"},
        ).json()["item"]
        admin_del = session.delete(
            url(f"/api/desk/items/{admin_item['id']}?nickname=root")
        )
        check(admin_del.status_code == 200, "管理员可删除他人物件", admin_del.status_code)

        # ------------------------------------------------------------------
        print("\n[6] 笔画：序列化 / 包围盒 / 输入校验")
        points = [[i * 2, (i % 7) * 3] for i in range(30)]
        stroke = session.post(
            url("/api/desk/items"),
            json={
                "nickname": "alice",
                "kind": "stroke",
                "x": 10,
                "y": 20,
                "body": json.dumps(points),
                "style": {"color": "#ff6b7a", "width": 4},
            },
        )
        check(stroke.status_code == 201, "创建笔画", stroke.status_code)
        stroke_item = j(stroke)["item"]
        check(stroke_item["w"] == 58 and stroke_item["h"] == 18, "服务端计算包围盒", (stroke_item["w"], stroke_item["h"]))
        check(json.loads(stroke_item["body"]) == points, "点序列紧凑回存")
        too_many = session.post(
            url("/api/desk/items"),
            json={"nickname": "alice", "kind": "stroke", "body": json.dumps([[i, i] for i in range(1600)])},
        )
        check(too_many.status_code == 400, "超长笔画被拒绝", too_many.status_code)
        bad_json = session.post(
            url("/api/desk/items"),
            json={"nickname": "alice", "kind": "stroke", "body": "{not json"},
        )
        check(bad_json.status_code == 400, "非法 JSON 被拒绝")
        bad_point = session.post(
            url("/api/desk/items"),
            json={"nickname": "alice", "kind": "stroke", "body": json.dumps([[1, "x"]])},
        )
        check(bad_point.status_code == 400, "非数字点被严格拒绝", bad_point.status_code)

        for payload, label in (
            ({"kind": "text", "body": ""}, "空便签"),
            ({"kind": "text", "body": "x" * 2001}, "超长便签"),
            ({"kind": "unknown", "body": "x"}, "未知类型"),
            ({"kind": "image"}, "缺少附件"),
        ):
            payload["nickname"] = "alice"
            res = session.post(url("/api/desk/items"), json=payload)
            check(res.status_code == 400, f"{label}被拒绝", res.status_code)

        # ------------------------------------------------------------------
        print("\n[7] 桌面文件上传 / 附件引用 / 安全响应头")
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        meta = json.dumps({"kind": "image", "x": 50, "y": 60, "w": 160, "h": 120, "body": "图注"})
        upload = session.post(
            url("/api/desk/items?nickname=alice"),
            data={"meta": meta},
            files={"file": ("示例图.png", io.BytesIO(png), "image/png")},
        )
        check(upload.status_code == 201, "multipart 放置图片", upload.status_code)
        uploaded = j(upload)["item"]
        check(uploaded["kind"] == "image", "自动识别为图片")
        check(uploaded["body"] == "图注", "图片说明被保存")
        att = uploaded["attachment"]
        file_res = session.get(url(att["url"] + "?nickname=alice"))
        check(file_res.status_code == 200 and file_res.content == png, "附件可下载且内容一致")
        check(file_res.headers.get("X-Content-Type-Options") == "nosniff", "文件带 nosniff")

        svg = session.post(
            url("/api/desk/items?nickname=alice"),
            data={"meta": json.dumps({"kind": "file"})},
            files={"file": ("bad.svg", io.BytesIO(b"<svg onload=alert(1)>"), "image/svg+xml")},
        )
        check(j(svg)["item"]["kind"] == "file", "SVG 强制降级为文件卡片")
        svg_res = session.get(url(j(svg)["item"]["attachment"]["url"] + "?nickname=alice"))
        check("attachment" in svg_res.headers.get("Content-Disposition", ""), "SVG 强制下载")

        # 桌面 → 线性：复用附件，不重复上传
        bridged = session.post(
            url("/api/messages?nickname=alice"),
            json={"attachment_id": att["id"], "text": "桌面图发到线性流"},
        )
        check(bridged.status_code == 201, "已有附件可引用进线性流", bridged.status_code)
        check(j(bridged)["message"]["attachment"]["id"] == att["id"], "引用的是同一个附件")

        # ------------------------------------------------------------------
        print("\n[8] 光标在线 / 视野 / 离开")
        cursor = session.post(
            url("/api/desk/cursor?nickname=bob"),
            json={
                "client_id": "tab_bob_0001",
                "x": 12,
                "y": 34,
                "cx": 400,
                "cy": 250,
                "zoom": 1.25,
                "vw": 800,
                "vh": 600,
                "tool": "pen",
            },
        )
        check(cursor.status_code == 200, "上报光标", cursor.status_code)
        presence = j(session.get(url("/api/desk/events?nickname=alice&since_rev=0")))["presence"]
        mine = next((c for c in presence if c["client_id"] == "tab_bob_0001"), None)
        check(bool(mine), "其他人能看到该光标")
        check(mine and mine["viewport"]["w"] == 800, "视野尺寸随心跳下发")
        check(mine and mine["tool"] == "pen", "当前工具随心跳下发")
        check(j(session.get(url("/api/group")))["group"]["desk_active_count"] >= 1, "群信息统计桌面活跃人数")
        session.post(url("/api/desk/leave?nickname=bob"), json={"client_id": "tab_bob_0001"})
        gone = j(session.get(url("/api/desk/events?nickname=alice&since_rev=0")))["presence"]
        check(not any(c["client_id"] == "tab_bob_0001" for c in gone), "离开后光标立刻消失")
        bad_cursor = session.post(
            url("/api/desk/cursor?nickname=bob"), json={"client_id": "../evil"}
        )
        check(bad_cursor.status_code == 400, "非法 client_id 被拒绝")

        # ------------------------------------------------------------------
        print("\n[9] 禁言：桌面写入受限、只读不受限")
        muted_identity = session.post(
            url("/api/admin/identities/bob?nickname=root"),
            json={"mute": {"seconds": 600}},
        )
        check(muted_identity.status_code == 200, "管理员禁言 bob", muted_identity.status_code)
        muted_create = session.post(
            url("/api/desk/items?nickname=bob"),
            json={"kind": "text", "body": "禁言测试"},
        )
        check(muted_create.status_code == 403, "禁言用户不能在桌面放东西", muted_create.status_code)
        check(j(muted_create)["error"]["code"] == "muted", "返回 muted 错误码")
        muted_move = session.patch(
            url(f"/api/desk/items/{item_id}?nickname=bob"), json={"x": 1}
        )
        check(muted_move.status_code == 403, "禁言用户不能移动物件")
        read_ok = session.get(url("/api/desk/snapshot?nickname=bob"))
        check(read_ok.status_code == 200, "禁言用户仍可浏览桌面")
        cursor_ok = session.post(
            url("/api/desk/cursor?nickname=bob"),
            json={"client_id": "tab_bob_0002", "x": 1, "y": 1},
        )
        check(cursor_ok.status_code == 200, "禁言用户仍可上报位置")

        # ------------------------------------------------------------------
        print("\n[10] 批量移动 / 删除（Ctrl 多选与框选）")
        batch_ids = []
        for i in range(3):
            created = session.post(
                url("/api/desk/items?nickname=alice"),
                json={"kind": "text", "x": 1000 + i * 50, "y": 900, "body": f"批量{i}"},
            )
            batch_ids.append(j(created)["item"]["id"])
        moved = session.post(
            url("/api/desk/items/batch?nickname=alice"),
            json={
                "action": "move",
                "items": [
                    {"id": batch_ids[0], "x": 1200, "y": 950},
                    {"id": batch_ids[1], "x": 1250, "y": 960},
                ],
            },
        )
        check(moved.status_code == 200, "批量移动接口可用", moved.status_code)
        moved_data = j(moved)
        check(moved_data.get("changed") == 2, "返回实际变更数", moved_data.get("changed"))
        check(
            len({row["rev"] for row in moved_data["items"]}) == 1,
            "整批共用一个版本号（增量只拉一次）",
        )
        positions = {
            row["id"]: (row["x"], row["y"])
            for row in j(session.get(url("/api/desk/snapshot?nickname=alice")))["items"]
        }
        check(positions.get(batch_ids[0]) == (1200.0, 950.0), "批量移动位置已持久化")
        check(positions.get(batch_ids[2]) == (1100.0, 900.0), "未列入批次的物件保持不动")
        forbidden_batch = session.post(
            url("/api/desk/items/batch?nickname=carol"),
            json={"action": "delete", "items": [{"id": i} for i in batch_ids]},
        )
        check(forbidden_batch.status_code == 403, "越权批量删除整批拒绝", forbidden_batch.status_code)
        check(
            j(forbidden_batch)["error"]["code"] == "forbidden",
            "越权返回 forbidden 而非禁言错误",
            j(forbidden_batch)["error"]["code"],
        )
        deleted_batch = session.post(
            url("/api/desk/items/batch?nickname=alice"),
            json={"action": "delete", "items": [{"id": i} for i in batch_ids[:2]]},
        )
        check(
            deleted_batch.status_code == 200 and j(deleted_batch).get("changed") == 2,
            "作者可一次删除多个选中物件",
            deleted_batch.status_code,
        )
        check(all(row["deleted"] for row in j(deleted_batch)["items"]), "返回墓碑供增量同步")
        check(
            batch_ids[2] in {
                row["id"]
                for row in j(session.get(url("/api/desk/snapshot?nickname=alice")))["items"]
            },
            "未选中的物件保留",
        )
        bad_action = session.post(
            url("/api/desk/items/batch?nickname=alice"),
            json={"action": "nope", "items": [{"id": 1}]},
        )
        check(bad_action.status_code == 400, "非法 action 被拒绝", bad_action.status_code)
        empty_batch = session.post(
            url("/api/desk/items/batch?nickname=alice"),
            json={"action": "move", "items": []},
        )
        check(empty_batch.status_code == 400, "空批次被拒绝", empty_batch.status_code)
        oversized = session.post(
            url("/api/desk/items/batch?nickname=alice"),
            json={"action": "move", "items": [{"id": 1, "x": 1, "y": 1}] * 101},
        )
        check(oversized.status_code == 400, "超过 100 个物件被拒绝", oversized.status_code)

        # ------------------------------------------------------------------
        print("\n[10b] 每张文字便签独立字号")
        font_a = j(session.post(
            url("/api/desk/items?nickname=alice"),
            json={
                "kind": "text", "x": -800, "y": 1200, "body": "字号 A",
                "style": {"color": "#ff8800", "note": "#112233", "fontSize": 30},
            },
        ))["item"]
        font_b = j(session.post(
            url("/api/desk/items?nickname=alice"),
            json={"kind": "text", "x": -500, "y": 1200, "body": "字号 B", "style": {"fontSize": 12}},
        ))["item"]
        check(font_a["style"].get("fontSize") == 30.0, "创建时可为单张便签指定字号")
        check(font_a["style"].get("color") == "#ff8800", "字号与颜色等字段共存")
        check(
            font_b["style"].get("fontSize") == 12.0 and font_a["style"]["fontSize"] == 30.0,
            "两张便签的字号互不影响",
        )
        changed = j(session.patch(
            url(f"/api/desk/items/{font_a['id']}?nickname=alice"),
            json={"style": {**font_a["style"], "fontSize": 44.5}},
        ))["item"]
        check(changed["style"].get("fontSize") == 44.5, "单张便签字号可修改")
        check(changed["style"].get("color") == "#ff8800", "改字号不会丢掉颜色 / 来源")
        forbidden_font = session.patch(
            url(f"/api/desk/items/{font_a['id']}?nickname=carol"),
            json={"style": {"fontSize": 60}},
        )
        check(forbidden_font.status_code == 403, "他人不能改这张便签的字号", forbidden_font.status_code)
        bad_font = j(session.patch(
            url(f"/api/desk/items/{font_a['id']}?nickname=alice"),
            json={"style": {"fontSize": "<script>alert(1)</script>"}},
        ))["item"]["style"]
        check(bad_font.get("fontSize") == 18.0, "非数字字号退回默认值，未注入 CSS")
        huge_font = j(session.patch(
            url(f"/api/desk/items/{font_a['id']}?nickname=alice"),
            json={"style": {"fontSize": 99999}},
        ))["item"]["style"]
        check(huge_font.get("fontSize") == 96.0, "超大字号被夹到上限", huge_font.get("fontSize"))
        stroke = j(session.post(
            url("/api/desk/items?nickname=alice"),
            json={"kind": "stroke", "x": -200, "y": 1200, "body": "[[0,0],[10,10]]"},
        ))["item"]
        batch_font = session.post(
            url("/api/desk/items/batch?nickname=alice"),
            json={
                "action": "font_size", "fontSize": 24,
                "items": [{"id": font_a["id"]}, {"id": font_b["id"]}, {"id": stroke["id"]}],
            },
        )
        batch_font_data = j(batch_font)
        check(batch_font.status_code == 200, "批量设置字号接口可用", batch_font.status_code)
        check(batch_font_data.get("changed") == 2, "非文字物件被跳过", batch_font_data.get("changed"))
        check(batch_font_data.get("font_size") == 24.0, "返回生效的字号")
        fonts = {
            row["id"]: (row.get("style") or {}).get("fontSize")
            for row in j(session.get(url("/api/desk/snapshot?nickname=alice")))["items"]
        }
        check(
            fonts.get(font_a["id"]) == 24.0 and fonts.get(font_b["id"]) == 24.0,
            "批量字号已持久化",
        )
        check(
            (fonts.get(stroke["id"]) or 18.0) == 18.0 or fonts.get(stroke["id"]) is None,
            "笔画未被写入字号",
        )
        forbidden_batch_font = session.post(
            url("/api/desk/items/batch?nickname=carol"),
            json={"action": "font_size", "fontSize": 40, "items": [{"id": font_a["id"]}]},
        )
        check(
            forbidden_batch_font.status_code == 403,
            "越权批量改字号整批拒绝",
            forbidden_batch_font.status_code,
        )

        # ------------------------------------------------------------------
        print("\n[11] CORS / 页面安全头")
        page = session.get(url("/"))
        csp = page.headers.get("Content-Security-Policy", "")
        check("script-src 'self'" in csp, "页面 CSP 未放宽为内联脚本")
        check("PATCH" in session.options(url("/api/desk/items")).headers.get(
            "Access-Control-Allow-Methods", ""
        ), "CORS 允许 PATCH")

        return 1 if failed else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    code = main()
    print("\n" + "=" * 58)
    print(f"通过 {passed} 项，失败 {len(failed)} 项")
    for name in failed:
        print("  -", name)
    print("=" * 58)
    sys.exit(code)
