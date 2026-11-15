"""把共享桌面里「飞到世界尽头」的物件找出来并搬回可见范围。

用法（在 chat5050 目录下）：

    # 只列出超范围物件，不改动
    python tools/desk_recenter.py --list

    # 真正搬移（走 HTTP API，在线客户端会通过 desk_rev 自动同步）
    python tools/desk_recenter.py --apply

    # 自定义范围 / 服务地址
    python tools/desk_recenter.py --apply --limit 10000 --base http://127.0.0.1:5050

搬移原则：
  * 只处理未删除的物件，已软删除的墓碑不动；
  * 空间上靠近的物件算同一簇，簇内保留相对位置（涂鸦不会被拆散）；
  * 完全重叠在同一坐标的物件错开成网格，搬回来之后能逐个点选 / 框选；
  * 新坐标保证 |x|、|y| 都不超过 limit。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import requests

DEFAULT_BASE = "http://127.0.0.1:5050"
DEFAULT_LIMIT = 10_000
DEFAULT_NICK = "root"

# 簇锚点：避开原数据里近处物件聚集的 x∈[-9500,1600] / y∈[-1400,3600]
ANCHORS = [
    (-1200.0, 4200.0),
    (2000.0, 4200.0),
    (2200.0, -1200.0),
    (6000.0, 1200.0),
]
CLUSTER_DISTANCE = 100_000.0  # 世界像素，超过这个距离算两簇
MAX_SPAN = 5_000.0            # 单簇保留的最大跨度
SAFE = 9_500.0                # 坐标硬上限，留一点余量


def api_session(base: str, nickname: str):
    session = requests.Session()
    session.trust_env = False  # 绕过环境里的 HTTP 代理
    session.headers.update({"X-Chat-Client": "api", "Accept": "application/json"})
    return session, f"{base.rstrip('/')}?nickname={nickname}"


def fetch_items(session, base, nickname):
    items, before = [], 2**62
    while True:
        resp = session.get(
            f"{base}/api/desk/snapshot",
            params={"nickname": nickname, "limit": 500, "before_id": before},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        items.extend(data["items"])
        if not data.get("has_more") or not data["items"]:
            break
        before = min(item["id"] for item in data["items"])
    return items


def cluster(items):
    """简单并查集：距离小于 CLUSTER_DISTANCE 的算同一簇。"""
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(items):
        for j in range(i + 1, len(items)):
            b = items[j]
            if abs(a["x"] - b["x"]) < CLUSTER_DISTANCE and abs(a["y"] - b["y"]) < CLUSTER_DISTANCE:
                parent[find(j)] = find(i)
    groups: dict[int, list] = {}
    for i, item in enumerate(items):
        groups.setdefault(find(i), []).append(item)
    return sorted(groups.values(), key=lambda g: min(i["x"] for i in g))


def plan(items, limit=DEFAULT_LIMIT):
    """返回 [(item, new_x, new_y)]，只处理 |x| 或 |y| 超过 limit 的物件。"""
    far = [i for i in items if abs(i["x"]) > limit or abs(i["y"]) > limit]
    moves = []
    bound = min(limit, SAFE)
    for index, group in enumerate(cluster(far)):
        min_x = min(i["x"] for i in group)
        min_y = min(i["y"] for i in group)
        span_x = max(i["x"] for i in group) - min_x
        span_y = max(i["y"] for i in group) - min_y
        factor = min(1.0, MAX_SPAN / max(span_x, span_y, 1.0))
        anchor_x, anchor_y = ANCHORS[index % len(ANCHORS)]
        # 同一相对坐标的物件依次错开，避免搬回来仍然完全重叠
        seen: dict[tuple[float, float], int] = {}
        for item in group:
            rel_x = round((item["x"] - min_x) * factor, 2)
            rel_y = round((item["y"] - min_y) * factor, 2)
            key = (rel_x, rel_y)
            same = seen.get(key, 0)
            seen[key] = same + 1
            x = max(-bound, min(bound, anchor_x + rel_x + (same % 5) * 700))
            y = max(-bound, min(bound, anchor_y + rel_y + (same // 5) * 500))
            moves.append((item, round(x, 2), round(y, 2)))
    return moves


def describe(item):
    kind = item["kind"]
    if kind == "stroke":
        return f"[笔画 · {len(item.get('body') or '')} 字符]"
    if kind in ("image", "file"):
        att = item.get("attachment") or {}
        return f"[{kind} · {att.get('name', '?')} · {att.get('size_human', '')}]"
    return (item.get("body") or "").replace("\n", " ⏎ ")


def main() -> int:
    ap = argparse.ArgumentParser(description="列出 / 搬回共享桌面上的超范围物件")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--nickname", default=DEFAULT_NICK)
    ap.add_argument("--limit", type=float, default=DEFAULT_LIMIT)
    ap.add_argument("--list", action="store_true", help="只列出，不改动")
    ap.add_argument("--apply", action="store_true", help="执行搬移")
    args = ap.parse_args()
    if not (args.list or args.apply):
        ap.error("请指定 --list 或 --apply")

    session, base = api_session(args.base, args.nickname)
    try:
        items = fetch_items(session, args.base, args.nickname)
    except Exception as exc:
        print(f"读取快照失败: {exc}", file=sys.stderr)
        return 2

    moves = plan(items, args.limit)
    print(f"服务: {args.base} | 桌面物件总数: {len(items)} | 超范围(|坐标|>{args.limit:g}): {len(moves)}")
    print("-" * 96)
    for item, x, y in moves:
        print(
            f"#{item['id']:<4} {item['kind']:<6} {item['nickname']:<10} "
            f"({item['x']:>13.2f}, {item['y']:>13.2f}) -> ({x:>9.2f}, {y:>9.2f})"
        )
        print(f"      内容: {describe(item)[:300]}")
    print("-" * 96)
    if args.list:
        print("仅列出，未改动（加 --apply 执行）")
        return 0
    if not moves:
        print("没有需要搬移的物件")
        return 0

    ok = 0
    for item, x, y in moves:
        resp = session.patch(
            f"{args.base}/api/desk/items/{item['id']}",
            params={"nickname": args.nickname},
            json={"x": x, "y": y},
            timeout=10,
        )
        if resp.status_code != 200 or not resp.json().get("ok"):
            print(f"✗ #{item['id']} 搬移失败: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
            continue
        ok += 1
        time.sleep(0.03)
    print(f"已搬移 {ok}/{len(moves)} 个物件")

    # 复查
    after = fetch_items(session, args.base, args.nickname)
    left = [i for i in after if abs(i["x"]) > args.limit or abs(i["y"]) > args.limit]
    print(f"复查：仍有 {len(left)} 个物件超出 ±{args.limit:g}")
    return 0 if ok == len(moves) and not left else 1


if __name__ == "__main__":
    sys.exit(main())
