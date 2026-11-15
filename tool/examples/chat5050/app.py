#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chat5050 · 极简单群聊服务
=========================

一个只有“大厅”一个群组的聊天服务：
  * 任何人只需取一个昵称，即可查看全部历史消息、发送文字 / 图片 / 文件；
  * 消息内容本身不提供修改接口，但支持“撤回”（隐藏，可随时撤销）；
  * 昵称即身份：管理员 / 普通用户、禁言 / 未禁言、锁定 / 未锁定；
  * 同时提供 Web 前端和 headless JSON API，二者读写同一份数据；
  * 使用 SQLite + 本地文件存储，默认监听 0.0.0.0:5050。
  * v2.1：新增「共享桌面」——同一份身份与附件之上的二维无限画布，
    可放置文字 / 图片 / 文件、自由涂鸦、平移缩放、看到他人实时光标；
    网页默认线性视图，可一键切换成桌面视图，两套内容互不覆盖。

权限模型（v2 新增）：
  * 初始只有 root 是管理员，并且 root 昵称默认锁定；
  * 管理员可以设置任何人的角色、禁言（定时 / 永久）、锁定昵称；
  * 锁定只约束 Web 前端（/api/login），带 X-Chat-Client: api 的 headless
    调用仍然可以自由使用任意昵称；
  * 任何人都能撤回自己的消息，管理员能撤回任何人的消息；
    撤回只是隐藏，随时可以撤销撤回。

运行：
    python app.py

环境变量：
    CHAT_HOST            监听地址（默认 0.0.0.0）
    CHAT_PORT            端口（默认 5050）
    CHAT_DATA_DIR        数据目录（默认 ./data）
    CHAT_MAX_UPLOAD_MB   单文件上限 MB（默认 50）
    CHAT_SECRET_KEY      Flask session 密钥（默认自动生成并持久化）
    CHAT_ROOT_NICKNAME   初始管理员昵称（默认 root）
    CHAT_ROOT_IP         root 锁定预绑定的 IP，逗号分隔（可选）
    CHAT_TRUST_PROXY     设为 1 时信任 X-Forwarded-For（反代后部署可选）
"""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_file,
    session,
)
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

APP_NAME = "Chat5050"
APP_VERSION = "2.1.0"
GROUP_NAME = "大厅"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CHAT_DATA_DIR", BASE_DIR / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "chat.db"
SECRET_PATH = DATA_DIR / "secret_key"

HOST = os.environ.get("CHAT_HOST", "0.0.0.0")
PORT = int(os.environ.get("CHAT_PORT", "5050"))
MAX_UPLOAD_MB = max(1, int(os.environ.get("CHAT_MAX_UPLOAD_MB", "50")))
MAX_UPLOAD = MAX_UPLOAD_MB * 1024 * 1024
MAX_TEXT = 4000
MAX_NICK = 24
PRESENCE_TTL = 75  # 秒：超过该时间没有请求则视为离线

# 共享桌面
DESK_MAX_TEXT = 2000           # 单张便签 / 单个文件说明的字符数
DESK_MAX_BODY = 120_000        # 笔画 JSON 上限（字节）
DESK_MAX_POINTS = 1500         # 单笔最多采样点
DESK_MAX_DIM = 20_000          # 物件尺寸上限（世界像素）
DESK_COORD_LIMIT = 5_000_000   # 坐标绝对值上限，防止溢出与画布失控
DESK_CURSOR_TTL = 20.0         # 桌面光标有效期（秒）
DESK_KINDS = ("text", "image", "file", "stroke")
DESK_BATCH_MAX = 100           # 一次批量移动 / 删除的物件数上限
# 每张文字便签独立字号（世界像素，随画布缩放；旧数据无该字段时前端按 18px 渲染）
DESK_NOTE_FONT_MIN = 10.0
DESK_NOTE_FONT_MAX = 96.0
DESK_NOTE_FONT_DEFAULT = 18.0

# 权限相关的初始值 / 开关
ROOT_NICKNAME = (os.environ.get("CHAT_ROOT_NICKNAME") or "root").strip() or "root"
ROOT_IPS = [
    ip.strip()
    for ip in (os.environ.get("CHAT_ROOT_IP") or "").split(",")
    if ip.strip()
]
TRUST_PROXY = os.environ.get("CHAT_TRUST_PROXY", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# 这些地址不做 IP 归属约束：本机调试 / 单机部署时所有用户都是 127.0.0.1，
# 如果照样绑定，root 一登录别人就再也不能用别的昵称了。
LOCK_IGNORE_IPS = {
    ip.strip()
    for ip in (
        os.environ.get("CHAT_LOCK_IGNORE_IPS", "127.0.0.1,::1,localhost")
        or ""
    ).split(",")
    if ip.strip()
}
# 供前端展示的禁言预设（秒），0 表示永久
MUTE_PRESETS = [
    {"label": "10 分钟", "seconds": 600},
    {"label": "1 小时", "seconds": 3600},
    {"label": "1 天", "seconds": 86400},
    {"label": "7 天", "seconds": 604800},
    {"label": "永久", "seconds": 0},
]

# 只有这些“栅格图片”允许在浏览器中内联展示，避免 SVG / HTML 造成 XSS。
INLINE_IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/bmp",
    "image/avif",
}

MSG_SELECT = """
    SELECT m.id, m.nickname, m.color, m.kind, m.body, m.created_at,
           m.attachment_id, m.recalled_at, m.recalled_by, m.recall_rev,
           a.original_name, a.mime, a.size
      FROM messages m
      LEFT JOIN attachments a ON a.id = m.attachment_id
"""

_BAD_INT = object()

DESK_SELECT = """
    SELECT i.*, a.original_name AS att_name, a.mime AS att_mime,
           a.size AS att_size
      FROM desk_items i
      LEFT JOIN attachments a ON a.id = i.attachment_id
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stored_name   TEXT    NOT NULL UNIQUE,
    original_name TEXT    NOT NULL,
    mime          TEXT    NOT NULL,
    size          INTEGER NOT NULL,
    sha256        TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname      TEXT    NOT NULL,
    color         TEXT    NOT NULL,
    kind          TEXT    NOT NULL CHECK (kind IN ('text', 'image', 'file')),
    body          TEXT    NOT NULL DEFAULT '',
    attachment_id INTEGER REFERENCES attachments(id),
    created_at    TEXT    NOT NULL,
    recalled_at   TEXT,
    recalled_by   TEXT,
    recall_rev    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_messages_id ON messages(id);

-- 共享桌面：每个物件是二维平面上的一个可变实体。
-- 与 messages 的 append-only 不同，桌面物件允许移动 / 编辑 / 软删除，
-- 靠全局 desk_rev + 每行 rev 做增量同步（后写覆盖，单进程内单调递增）。
CREATE TABLE IF NOT EXISTS desk_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT    NOT NULL CHECK (kind IN ('text', 'image', 'file', 'stroke')),
    nickname      TEXT    NOT NULL,
    color         TEXT    NOT NULL,
    x             REAL    NOT NULL,
    y             REAL    NOT NULL,
    w             REAL    NOT NULL DEFAULT 0,
    h             REAL    NOT NULL DEFAULT 0,
    z             INTEGER NOT NULL DEFAULT 0,
    body          TEXT    NOT NULL DEFAULT '',
    style         TEXT    NOT NULL DEFAULT '{}',
    attachment_id INTEGER REFERENCES attachments(id),
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    updated_by    TEXT,
    rev           INTEGER NOT NULL DEFAULT 0,
    deleted       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_desk_items_rev ON desk_items(rev);

CREATE TABLE IF NOT EXISTS presence (
    nickname   TEXT PRIMARY KEY,
    color      TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);

-- 昵称即身份：一行代表一个昵称的角色 / 禁言 / 锁定状态
CREATE TABLE IF NOT EXISTS identities (
    nickname      TEXT PRIMARY KEY,
    role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    muted_until   TEXT,
    muted_forever INTEGER NOT NULL DEFAULT 0,
    muted_at      TEXT,
    muted_by      TEXT,
    locked        INTEGER NOT NULL DEFAULT 0,
    locked_at     TEXT,
    locked_by     TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 昵称出现过的 IP（只记录 Web 登录，用于锁定时的归属判断）
CREATE TABLE IF NOT EXISTS identity_ips (
    nickname   TEXT NOT NULL,
    ip         TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    PRIMARY KEY (nickname, ip)
);

-- 锁定快照：锁定昵称时该昵称归属的 IP，这些 IP 不能再换成别的昵称
CREATE TABLE IF NOT EXISTS lock_ips (
    nickname TEXT NOT NULL,
    ip       TEXT NOT NULL,
    bound_at TEXT NOT NULL,
    PRIMARY KEY (nickname, ip)
);

CREATE INDEX IF NOT EXISTS idx_lock_ips_ip ON lock_ips(ip);

-- 全局状态版本号：撤回 / 权限变更时自增，用于长轮询与增量同步
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
    version TEXT PRIMARY KEY
);

INSERT OR IGNORE INTO schema_version (version) VALUES ('1');
INSERT OR IGNORE INTO schema_version (version) VALUES ('2');
INSERT OR IGNORE INTO schema_version (version) VALUES ('3');
INSERT OR IGNORE INTO settings (key, value) VALUES ('state_rev', '0');
INSERT OR IGNORE INTO settings (key, value) VALUES ('desk_rev', '0');
"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def now_iso() -> str:
    """UTC ISO8601，精确到秒，例如 2026-10-18T07:30:00Z。"""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def iso_ago(seconds: int) -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        - timedelta(seconds=seconds)
    ).isoformat().replace("+00:00", "Z")


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{num} B"


_NICK_CTRL = re.compile(
    r"[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069\ufeff]"
)


def normalize_nickname(raw) -> tuple[str | None, str | None]:
    """规范化昵称，返回 (nickname, error_message)。"""
    if raw is None:
        return None, "请先设置昵称"
    if not isinstance(raw, str):
        return None, "昵称格式不正确"
    text = unicodedata.normalize("NFKC", raw)
    text = _NICK_CTRL.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None, "昵称不能为空"
    if len(text) > MAX_NICK:
        return None, f"昵称最多 {MAX_NICK} 个字符"
    return text, None


def normalize_text(raw) -> tuple[str | None, str | None]:
    if raw is None:
        return "", None
    if not isinstance(raw, str):
        return None, "消息内容格式不正确"
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = text.strip()
    if not text:
        return "", None
    if len(text) > MAX_TEXT:
        return None, f"消息最多 {MAX_TEXT} 个字符"
    return text, None


def clean_filename(raw: str | None) -> str:
    """仅用于展示/下载的原始文件名，去掉路径与控制字符。"""
    name = unicodedata.normalize("NFC", raw or "")
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(
        ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf")
    )
    name = name.strip().strip(".").strip()
    if not name:
        name = "file"
    if len(name) > 120:
        suffix = Path(name).suffix[:12]
        name = name[: 120 - len(suffix)] + suffix
    return name


def safe_ext(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,10}", ext) else ""


def hsl_to_hex(h: float, s: float, light: float) -> str:
    """HSL -> #rrggbb，用于给每个昵称生成稳定颜色。"""
    c = (1 - abs(2 * light - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = light - c / 2
    if h < 60:
        r, g, b = c, x, 0
    elif h < 120:
        r, g, b = x, c, 0
    elif h < 180:
        r, g, b = 0, c, x
    elif h < 240:
        r, g, b = 0, x, c
    elif h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x
    return "#{:02x}{:02x}{:02x}".format(
        int((r + m) * 255 + 0.5),
        int((g + m) * 255 + 0.5),
        int((b + m) * 255 + 0.5),
    )


def color_for(nickname: str) -> str:
    digest = hashlib.sha256(nickname.encode("utf-8")).digest()
    hue = int.from_bytes(digest[:4], "big") % 360
    # 深色背景上较醒目的饱和度/亮度
    return hsl_to_hex(hue, 0.66, 0.62)


def guess_mime(filename: str, provided: str | None) -> str:
    mime = (provided or "").split(";")[0].strip().lower()
    if not mime or mime == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            mime = guessed.lower()
    return mime or "application/octet-stream"


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------

def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=15.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 15000")
    return con


@contextmanager
def db():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}


def init_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
        con.executescript(SCHEMA)

        # ---- v1 -> v2 迁移：给已有 messages 表补上撤回字段 ----
        message_columns = _table_columns(con, "messages")
        for column, ddl in (
            ("recalled_at", "ALTER TABLE messages ADD COLUMN recalled_at TEXT"),
            ("recalled_by", "ALTER TABLE messages ADD COLUMN recalled_by TEXT"),
            (
                "recall_rev",
                "ALTER TABLE messages ADD COLUMN recall_rev INTEGER NOT NULL DEFAULT 0",
            ),
        ):
            if column not in message_columns:
                con.execute(ddl)

        # ---- 把历史昵称补进 identities，方便管理员在面板里统一管理 ----
        stamp = now_iso()
        con.execute(
            """
            INSERT OR IGNORE INTO identities (nickname, role, created_at, updated_at)
            SELECT DISTINCT nickname, 'user', ?, ? FROM messages
            """,
            (stamp, stamp),
        )
        con.execute(
            """
            INSERT OR IGNORE INTO identities (nickname, role, created_at, updated_at)
            SELECT nickname, 'user', ?, ? FROM presence
            """,
            (stamp, stamp),
        )

        # ---- 初始唯一的超级管理员：root（默认锁定） ----
        con.execute(
            """
            INSERT OR IGNORE INTO identities
                (nickname, role, locked, locked_at, locked_by, created_at, updated_at)
            VALUES (?, 'admin', 1, ?, 'system', ?, ?)
            """,
            (ROOT_NICKNAME, stamp, stamp, stamp),
        )
        for ip in ROOT_IPS:
            con.execute(
                "INSERT OR IGNORE INTO lock_ips (nickname, ip, bound_at) VALUES (?, ?, ?)",
                (ROOT_NICKNAME, ip, stamp),
            )

        con.execute(
            "DELETE FROM presence WHERE last_seen < ?", (iso_ago(7 * 24 * 3600),)
        )

    # 清理上次异常退出遗留的半成品上传文件
    for part in UPLOAD_DIR.glob("*.part"):
        try:
            part.unlink()
        except OSError:
            pass


def load_secret_key() -> str:
    env_key = os.environ.get("CHAT_SECRET_KEY")
    if env_key:
        return env_key
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if SECRET_PATH.exists():
            key = SECRET_PATH.read_text(encoding="utf-8").strip()
            if key:
                return key
        key = secrets.token_hex(32)
        SECRET_PATH.write_text(key, encoding="utf-8")
        return key
    except OSError:
        return secrets.token_hex(32)


# 新消息 / 权限变更唤醒条件变量：长轮询靠它做到“有变化立刻返回”。
_new_message = threading.Condition()


def notify_new_message() -> None:
    with _new_message:
        _new_message.notify_all()


def read_state_rev(con: sqlite3.Connection) -> int:
    """读取全局状态版本号；任何撤回 / 权限变更都会让它自增。"""
    row = con.execute("SELECT value FROM settings WHERE key = 'state_rev'").fetchone()
    if not row:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def read_state_rev_now() -> int:
    with db() as con:
        return read_state_rev(con)


def bump_state_rev() -> int:
    """推进全局版本号，并唤醒所有等待中的长轮询。"""
    with db() as con:
        con.execute(
            """
            INSERT INTO settings (key, value) VALUES ('state_rev', '1')
            ON CONFLICT(key) DO UPDATE
               SET value = CAST(CAST(settings.value AS INTEGER) + 1 AS TEXT)
            """
        )
        rev = read_state_rev(con)
    notify_new_message()
    return rev


def wait_for_changes(after_id: int, since_rev: int, timeout: float) -> None:
    """阻塞，直到出现 id > after_id 的消息、全局版本号变化，或超时。"""
    if timeout <= 0:
        return
    deadline = time.monotonic() + min(timeout, 25.0)
    with _new_message:
        while True:
            with db() as con:
                row = con.execute("SELECT MAX(id) AS m FROM messages").fetchone()
                rev = read_state_rev(con)
            if (row["m"] or 0) > after_id or rev > since_rev:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            # 最多睡 5 秒，避免极端情况下漏掉唤醒
            _new_message.wait(min(remaining, 5.0))


# ---------------------------------------------------------------------------
# 在线状态
# ---------------------------------------------------------------------------

_presence_lock = threading.Lock()
_presence_last: dict[str, float] = {}


def touch_presence(nickname: str, color: str | None = None) -> None:
    """记录昵称活跃，10 秒内重复调用只写一次库。"""
    now = time.monotonic()
    with _presence_lock:
        if now - _presence_last.get(nickname, 0.0) < 10:
            return
        _presence_last[nickname] = now
    stamp = now_iso()
    with db() as con:
        con.execute(
            """
            INSERT INTO presence (nickname, color, first_seen, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(nickname) DO UPDATE SET
                color = excluded.color,
                last_seen = excluded.last_seen
            """,
            (nickname, color or color_for(nickname), stamp, stamp),
        )


def online_users() -> list[dict]:
    cutoff = iso_ago(PRESENCE_TTL)
    with db() as con:
        rows = con.execute(
            """
            SELECT nickname, color, last_seen
              FROM presence
             WHERE last_seen >= ?
             ORDER BY last_seen DESC
             LIMIT 200
            """,
            (cutoff,),
        ).fetchall()
    return [
        {"nickname": r["nickname"], "color": r["color"], "last_seen": r["last_seen"]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 身份与权限：昵称是本系统唯一的身份凭据
# ---------------------------------------------------------------------------

def human_duration(seconds: int) -> str:
    """把秒数说人话：2 天 3 小时 / 5 分 12 秒。"""
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days} 天 {hours} 小时" if hours else f"{days} 天"
    if hours:
        return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"
    if minutes:
        return f"{minutes} 分 {seconds} 秒" if seconds else f"{minutes} 分"
    return f"{seconds} 秒"


def client_ip() -> str:
    """获取客户端 IP；只有显式信任反向代理时才读 X-Forwarded-For。"""
    if TRUST_PROXY:
        forwarded = request.headers.get("X-Forwarded-For", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    return (request.remote_addr or "").strip()[:64]


def _identity_row(con: sqlite3.Connection, nickname: str):
    return con.execute(
        "SELECT * FROM identities WHERE nickname = ?", (nickname,)
    ).fetchone()


def ensure_identity(nickname: str) -> None:
    """昵称第一次真正参与聊天时建档（读消息不建档，避免刷出垃圾名单）。"""
    stamp = now_iso()
    with db() as con:
        con.execute(
            """
            INSERT OR IGNORE INTO identities (nickname, role, created_at, updated_at)
            VALUES (?, 'user', ?, ?)
            """,
            (nickname, stamp, stamp),
        )


def mute_state(row) -> tuple[bool, str | None, int | None]:
    """返回 (是否禁言中, 到期时间, 剩余秒数)；永久禁言时剩余秒数为 None。"""
    if row is None:
        return False, None, None
    if row["muted_forever"]:
        return True, None, None
    until = row["muted_until"]
    if not until:
        return False, None, None
    try:
        end = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
    except ValueError:
        return False, until, None
    remaining = int((end - datetime.now(timezone.utc)).total_seconds())
    if remaining <= 0:
        return False, until, 0
    return True, until, remaining


def identity_view(nickname: str) -> dict:
    """当前身份快照，前端据此切换按钮 / 禁言横幅。"""
    with db() as con:
        row = _identity_row(con, nickname)
    role = (row["role"] if row else "user") or "user"
    muted, until, remaining = mute_state(row)
    return {
        "nickname": nickname,
        "role": role,
        "is_admin": role == "admin",
        "muted": muted,
        "muted_until": until,
        "muted_forever": bool(row["muted_forever"]) if row else False,
        "mute_remaining": remaining,
        "locked": bool(row["locked"]) if row else False,
    }


def is_admin(nickname: str) -> bool:
    with db() as con:
        row = _identity_row(con, nickname)
    return bool(row) and row["role"] == "admin"


def admin_nicknames() -> list[str]:
    with db() as con:
        rows = con.execute(
            "SELECT nickname FROM identities WHERE role = 'admin' ORDER BY nickname"
        ).fetchall()
    return [row["nickname"] for row in rows]


def muted_response(nickname: str):
    """禁言中的用户发言 / 上传时返回 403；未禁言返回 None。"""
    ident = identity_view(nickname)
    if not ident["muted"]:
        return None
    if ident["muted_forever"]:
        message = "你已被永久禁言，暂时不能发言"
    else:
        message = f"你已被禁言，还需等待 {human_duration(ident['mute_remaining'] or 0)}"
    resp = jsonify(
        ok=False,
        error={
            "code": "muted",
            "message": message,
            "until": ident["muted_until"],
            "permanent": ident["muted_forever"],
            "remaining": ident["mute_remaining"],
        },
    )
    resp.status_code = 403
    return resp


def record_identity_ip(nickname: str, ip: str) -> None:
    """记录昵称出现过的 Web 登录 IP，供锁定昵称时做归属快照。"""
    if not ip or ip in LOCK_IGNORE_IPS:
        return
    stamp = now_iso()
    with db() as con:
        con.execute(
            """
            INSERT INTO identity_ips (nickname, ip, first_seen, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(nickname, ip) DO UPDATE SET last_seen = excluded.last_seen
            """,
            (nickname, ip, stamp, stamp),
        )


def check_nickname_lock(nickname: str, ip: str) -> tuple[bool, str, str]:
    """Web 前端登录时的昵称锁定校验，返回 (是否允许, 错误码, 提示语)。

    规则：
      1) 昵称已锁定，但锁定快照为空 —— 视为“首次认领”，把当前 IP 绑上去；
      2) 昵称已锁定，当前 IP 不在快照里 —— 拒绝，别人不能冒用这个昵称；
      3) 当前 IP 已绑定在某个锁定昵称上，却想换用别的昵称 —— 拒绝。

    127.0.0.1 等本机地址默认豁免（见 CHAT_LOCK_IGNORE_IPS）。
    """
    if ip in LOCK_IGNORE_IPS:
        return True, "", ""

    with db() as con:
        row = _identity_row(con, nickname)
        locked = bool(row) and bool(row["locked"])
        bound = [
            r["ip"]
            for r in con.execute(
                "SELECT ip FROM lock_ips WHERE nickname = ?", (nickname,)
            ).fetchall()
        ]
        others = []
        if not locked:
            others = con.execute(
                """
                SELECT DISTINCT l.nickname
                  FROM lock_ips l
                  JOIN identities i ON i.nickname = l.nickname
                 WHERE l.ip = ? AND i.locked = 1 AND l.nickname <> ?
                 ORDER BY l.nickname
                """,
                (ip, nickname),
            ).fetchall()

    if locked:
        if not bound:
            # 锁定快照为空（例如刚初始化的 root）：首次登录即认领该 IP
            with db() as con:
                con.execute(
                    "INSERT OR IGNORE INTO lock_ips (nickname, ip, bound_at) VALUES (?, ?, ?)",
                    (nickname, ip, now_iso()),
                )
            return True, "", ""
        if ip in bound:
            return True, "", ""
        return (
            False,
            "nickname_locked",
            f"昵称「{nickname}」已被锁定，只有它自己的 IP 可以使用",
        )

    if others:
        owner = others[0]["nickname"]
        return (
            False,
            "ip_locked",
            f"当前 IP 已绑定昵称「{owner}」，不能切换成其他昵称",
        )
    return True, "", ""


def _person_dict(row, ips: list[dict], pins: list[str], message_count: int, last_seen) -> dict:
    muted, until, remaining = mute_state(row)
    cutoff = iso_ago(PRESENCE_TTL)
    return {
        "nickname": row["nickname"],
        "role": row["role"],
        "is_admin": row["role"] == "admin",
        "muted": muted,
        "muted_until": until,
        "muted_forever": bool(row["muted_forever"]),
        "mute_remaining": remaining,
        "muted_by": row["muted_by"],
        "muted_at": row["muted_at"],
        "locked": bool(row["locked"]),
        "locked_at": row["locked_at"],
        "locked_by": row["locked_by"],
        "locked_ips": pins,
        "ips": [item["ip"] for item in ips][:10],
        "ip_count": len(ips),
        "message_count": message_count,
        "last_seen": last_seen,
        "online": bool(last_seen and last_seen >= cutoff),
        "created_at": row["created_at"],
    }


def ensure_presence_identities() -> None:
    """把只在 presence 里出现过的昵称也补进 identities，管理员才能管理到。"""
    stamp = now_iso()
    with db() as con:
        con.execute(
            """
            INSERT OR IGNORE INTO identities (nickname, role, created_at, updated_at)
            SELECT nickname, 'user', ?, ? FROM presence
            """,
            (stamp, stamp),
        )


def identity_roster() -> list[dict]:
    """管理员面板用的完整名单：角色 / 禁言 / 锁定 / IP / 最近活跃。"""
    ensure_presence_identities()
    with db() as con:
        rows = con.execute(
            """
            SELECT i.nickname, i.role, i.muted_until, i.muted_forever,
                   i.muted_at, i.muted_by, i.locked, i.locked_at, i.locked_by,
                   i.created_at, i.updated_at,
                   (SELECT COUNT(*) FROM messages m WHERE m.nickname = i.nickname)
                       AS message_count,
                   (SELECT MAX(p.last_seen) FROM presence p
                     WHERE p.nickname = i.nickname) AS last_seen
              FROM identities i
             ORDER BY (i.role = 'admin') DESC, i.nickname COLLATE NOCASE ASC
             LIMIT 500
            """
        ).fetchall()
        ip_rows = con.execute(
            "SELECT nickname, ip, last_seen FROM identity_ips ORDER BY last_seen DESC"
        ).fetchall()
        lock_rows = con.execute(
            "SELECT nickname, ip FROM lock_ips ORDER BY ip"
        ).fetchall()

    seen_ips: dict[str, list[dict]] = {}
    for row in ip_rows:
        seen_ips.setdefault(row["nickname"], []).append(
            {"ip": row["ip"], "last_seen": row["last_seen"]}
        )
    pinned: dict[str, list[str]] = {}
    for row in lock_rows:
        pinned.setdefault(row["nickname"], []).append(row["ip"])

    return [
        _person_dict(
            row,
            seen_ips.get(row["nickname"], []),
            pinned.get(row["nickname"], []),
            row["message_count"],
            row["last_seen"],
        )
        for row in rows
    ]


def identity_detail(nickname: str) -> dict:
    with db() as con:
        row = _identity_row(con, nickname)
        ips = [
            {"ip": r["ip"], "last_seen": r["last_seen"]}
            for r in con.execute(
                "SELECT ip, last_seen FROM identity_ips WHERE nickname = ? "
                "ORDER BY last_seen DESC",
                (nickname,),
            ).fetchall()
        ]
        pins = [
            r["ip"]
            for r in con.execute(
                "SELECT ip FROM lock_ips WHERE nickname = ? ORDER BY ip", (nickname,)
            ).fetchall()
        ]
        message_count = con.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE nickname = ?", (nickname,)
        ).fetchone()["c"]
        last_seen = con.execute(
            "SELECT MAX(last_seen) AS l FROM presence WHERE nickname = ?", (nickname,)
        ).fetchone()["l"]
    if row is None:
        stamp = now_iso()
        row = {
            "nickname": nickname,
            "role": "user",
            "muted_until": None,
            "muted_forever": 0,
            "muted_at": None,
            "muted_by": None,
            "locked": 0,
            "locked_at": None,
            "locked_by": None,
            "created_at": stamp,
            "updated_at": stamp,
        }
    return _person_dict(row, ips, pins, message_count, last_seen)


# ---------------------------------------------------------------------------
# 限流：昵称为键的滑动窗口，防止脚本瞬间刷屏
# ---------------------------------------------------------------------------

_rate_lock = threading.Lock()
_rate_events: dict[str, deque[float]] = {}


def rate_limited(key: str, limit: int = 20, window: float = 10.0) -> bool:
    now = time.monotonic()
    with _rate_lock:
        bucket = _rate_events.setdefault(key, deque())
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= limit:
            return True
        bucket.append(now)
        # 简单清理，避免字典无限增长
        if len(_rate_events) > 2000:
            stale_cutoff = now - 60.0
            for k in [
                k
                for k, v in _rate_events.items()
                if not v or v[-1] < stale_cutoff
            ]:
                _rate_events.pop(k, None)
    return False


# ---------------------------------------------------------------------------
# Flask 应用
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = load_secret_key()
app.config.update(
    MAX_CONTENT_LENGTH=MAX_UPLOAD + 2 * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_NAME="chat5050",
    JSON_SORT_KEYS=False,
)
app.json.ensure_ascii = False


def json_body() -> dict:
    if not request.is_json:
        return {}
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def api_error(code: str, message: str, status: int = 400):
    resp = jsonify(ok=False, error={"code": code, "message": message})
    resp.status_code = status
    return resp


def raw_nickname(payload: dict | None = None):
    """
    按优先级读取请求中的原始昵称；完全没有提供时返回 None。
      1. URL 查询参数 ?nickname=
      2. 请求体 nickname（JSON 或 form）
      3. 请求头 X-Nickname（按 UTF-8 解码，兼容中文）
      4. Web 登录后的 session
    """
    if "nickname" in request.args:
        return request.args.get("nickname")
    if payload is not None and "nickname" in payload:
        return payload.get("nickname")
    if "nickname" in request.form:
        return request.form.get("nickname")
    header = request.headers.get("X-Nickname")
    if header is not None:
        try:
            return header.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return header
    return session.get("nickname")


def require_user(payload: dict | None = None):
    raw = raw_nickname(payload)
    if raw is None:
        return None, api_error(
            "nickname_required",
            "请先设置昵称。可通过 ?nickname=、请求体 nickname、X-Nickname 请求头"
            "或先调用 /api/login 设置。",
            401,
        )
    nickname, error = normalize_nickname(raw)
    if error:
        return None, api_error("invalid_nickname", error, 400)
    touch_presence(nickname)
    return nickname, None


def attachment_dict(row) -> dict | None:
    if not row["attachment_id"]:
        return None
    att_id = row["attachment_id"]
    mime = row["mime"] or "application/octet-stream"
    return {
        "id": att_id,
        "name": row["original_name"],
        "mime": mime,
        "size": row["size"],
        "size_human": human_size(row["size"] or 0),
        "is_image": mime in INLINE_IMAGE_MIMES,
        "url": f"/api/files/{att_id}",
        "download_url": f"/api/files/{att_id}?download=1",
    }


def message_dict(row, viewer: str | None = None, admin: bool = False) -> dict:
    """消息对外表示。撤回的消息只保留“已被撤回”的事实，不泄露内容。"""
    recalled = bool(row["recalled_at"])
    data = {
        "id": row["id"],
        "nickname": row["nickname"],
        "color": row["color"],
        "kind": row["kind"],
        "text": "" if recalled else row["body"],
        "created_at": row["created_at"],
        "attachment": None if recalled else attachment_dict(row),
        "recalled": recalled,
        "recalled_by": row["recalled_by"],
        "recalled_at": row["recalled_at"],
        "recall_rev": row["recall_rev"] or 0,
        "own": bool(viewer) and viewer == row["nickname"],
        "can_recall": False,
        "can_unrecall": False,
    }
    if viewer:
        if recalled:
            data["can_unrecall"] = bool(
                admin or row["recalled_by"] == viewer
            )
        else:
            data["can_recall"] = bool(admin or data["own"])
    return data


def recall_state(row) -> dict:
    """撤回状态的最小增量，用于长轮询时同步历史消息。"""
    return {
        "id": row["id"],
        "recalled": bool(row["recalled_at"]),
        "recalled_by": row["recalled_by"],
        "recalled_at": row["recalled_at"],
        "recall_rev": row["recall_rev"] or 0,
    }


def fetch_message(message_id: int):
    with db() as con:
        return con.execute(MSG_SELECT + " WHERE m.id = ?", (message_id,)).fetchone()


def insert_text_message(nickname: str, text: str):
    color = color_for(nickname)
    stamp = now_iso()
    with db() as con:
        cur = con.execute(
            """
            INSERT INTO messages (nickname, color, kind, body, attachment_id, created_at)
            VALUES (?, ?, 'text', ?, NULL, ?)
            """,
            (nickname, color, text, stamp),
        )
        message_id = cur.lastrowid
    notify_new_message()
    return fetch_message(message_id)


def insert_attachment_message(
    nickname: str, text: str, attachment_id: int, kind: str
):
    """桌面 → 线性：复用已上传的附件，不再传一遍文件。"""
    color = color_for(nickname)
    stamp = now_iso()
    with db() as con:
        cur = con.execute(
            """
            INSERT INTO messages (nickname, color, kind, body, attachment_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (nickname, color, kind, text, attachment_id, stamp),
        )
        message_id = cur.lastrowid
    notify_new_message()
    return fetch_message(message_id)


def group_info() -> dict:
    with db() as con:
        count = con.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
        attachment_count = con.execute(
            "SELECT COUNT(*) AS c FROM attachments"
        ).fetchone()["c"]
        recalled_count = con.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE recalled_at IS NOT NULL"
        ).fetchone()["c"]
        admin_count = con.execute(
            "SELECT COUNT(*) AS c FROM identities WHERE role = 'admin'"
        ).fetchone()["c"]
        desk_item_count = con.execute(
            "SELECT COUNT(*) AS c FROM desk_items WHERE deleted = 0"
        ).fetchone()["c"]
    people = online_users()
    return {
        "name": GROUP_NAME,
        "message_count": count,
        "recalled_count": recalled_count,
        "attachment_count": attachment_count,
        "online_count": len(people),
        "admin_count": admin_count,
        "desk_item_count": desk_item_count,
        "desk_active_count": desk_active_count(),
        "max_upload_mb": MAX_UPLOAD_MB,
        "max_text": MAX_TEXT,
        "max_nickname": MAX_NICK,
        "mute_presets": MUTE_PRESETS,
    }


# ---------------------------------------------------------------------------
# 撤回 / 撤销撤回
# ---------------------------------------------------------------------------

def set_recall(
    message_id: int, recalled: bool, actor: str, admin: bool
) -> tuple[object | None, str, str]:
    """撤回 = 隐藏（不改动原文），撤销撤回 = 重新显示。

    返回 (消息行, 错误码, 错误信息)。重复操作是幂等的。
    """
    row = fetch_message(message_id)
    if not row:
        return None, "not_found", "消息不存在"
    if not admin and row["nickname"] != actor:
        return None, "forbidden", "只能撤回自己的发言"

    if recalled:
        if row["recalled_at"]:
            return row, "", ""
    else:
        if not row["recalled_at"]:
            return row, "", ""
        if not admin and row["recalled_by"] != actor:
            return None, "forbidden", "只能撤销自己执行的撤回"

    stamp = now_iso()
    with db() as con:
        con.execute(
            """
            INSERT INTO settings (key, value) VALUES ('state_rev', '1')
            ON CONFLICT(key) DO UPDATE
               SET value = CAST(CAST(settings.value AS INTEGER) + 1 AS TEXT)
            """
        )
        rev = read_state_rev(con)
        if recalled:
            con.execute(
                """
                UPDATE messages
                   SET recalled_at = ?, recalled_by = ?, recall_rev = ?
                 WHERE id = ?
                """,
                (stamp, actor, rev, message_id),
            )
        else:
            con.execute(
                """
                UPDATE messages
                   SET recalled_at = NULL, recalled_by = NULL, recall_rev = ?
                 WHERE id = ?
                """,
                (rev, message_id),
            )
    notify_new_message()
    return fetch_message(message_id), "", ""


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template(
        "index.html",
        group_name=GROUP_NAME,
        max_upload_mb=MAX_UPLOAD_MB,
        max_text=MAX_TEXT,
        max_nickname=MAX_NICK,
        version=APP_VERSION,
    )


@app.get("/favicon.ico")
def favicon():
    return send_file(BASE_DIR / "static" / "favicon.svg", mimetype="image/svg+xml")


# ---------------------------------------------------------------------------
# 身份 / 群信息
# ---------------------------------------------------------------------------

@app.post("/api/login")
def api_login():
    payload = json_body()
    raw = (
        payload.get("nickname")
        if "nickname" in payload
        else (request.form.get("nickname") or request.args.get("nickname"))
    )
    nickname, error = normalize_nickname(raw)
    if error:
        return api_error("invalid_nickname", error, 400)

    # 锁定只约束 Web 前端：headless 客户端显式声明 X-Chat-Client: api
    # （或 JSON 里带 "api": true）即可自由使用任意昵称。
    api_mode = (
        request.headers.get("X-Chat-Client", "").strip().lower() == "api"
        or payload.get("api") is True
    )
    ip = client_ip()
    if not api_mode:
        allowed, code, message = check_nickname_lock(nickname, ip)
        if not allowed:
            return api_error(code, message, 403)

    session.permanent = True
    session["nickname"] = nickname
    ensure_identity(nickname)
    if not api_mode:
        record_identity_ip(nickname, ip)
    touch_presence(nickname)
    return jsonify(
        ok=True,
        nickname=nickname,
        color=color_for(nickname),
        viewer=identity_view(nickname),
        group=group_info(),
    )


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def api_me():
    nickname, error = require_user()
    if error:
        return error
    ensure_identity(nickname)
    return jsonify(
        ok=True,
        nickname=nickname,
        color=color_for(nickname),
        viewer=identity_view(nickname),
        group=group_info(),
    )


@app.get("/api/group")
def api_group():
    return jsonify(ok=True, group=group_info())


@app.get("/api/presence")
def api_presence():
    nickname, error = require_user()
    if error:
        return error
    people = online_users()
    return jsonify(ok=True, online_count=len(people), online=people)


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------

@app.get("/api/messages")
def api_messages():
    nickname, error = require_user()
    if error:
        return error

    def as_int(name: str, default=None):
        raw = request.args.get(name)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            return _BAD_INT

    after_id = as_int("after_id")
    if after_id is None:
        after_id = as_int("since")
    before_id = as_int("before_id")
    limit = as_int("limit", 50)
    wait = as_int("wait", 0)
    since_rev = as_int("since_rev", 0)
    if _BAD_INT in (after_id, before_id, limit, wait, since_rev):
        return api_error(
            "bad_request",
            "after_id / before_id / limit / wait / since_rev 必须是整数",
            400,
        )
    if after_id is not None and after_id < 0:
        return api_error("bad_request", "after_id 不能为负数", 400)
    if since_rev < 0:
        return api_error("bad_request", "since_rev 不能为负数", 400)
    limit = max(1, min(limit, 200))
    wait = max(0, min(wait, 25))

    if after_id is not None and wait:
        wait_for_changes(after_id, since_rev, wait)

    with db() as con:
        if after_id is not None:
            rows = con.execute(
                MSG_SELECT + " WHERE m.id > ? ORDER BY m.id ASC LIMIT ?",
                (after_id, limit),
            ).fetchall()
            has_more = False
        elif before_id is not None:
            rows = con.execute(
                MSG_SELECT + " WHERE m.id < ? ORDER BY m.id DESC LIMIT ?",
                (before_id, limit),
            ).fetchall()
            rows = list(reversed(rows))
            has_more = bool(rows) and bool(
                con.execute(
                    "SELECT 1 FROM messages WHERE id < ? LIMIT 1", (rows[0]["id"],)
                ).fetchone()
            )
        else:
            rows = con.execute(
                MSG_SELECT + " ORDER BY m.id DESC LIMIT ?", (limit,)
            ).fetchall()
            rows = list(reversed(rows))
            has_more = bool(rows) and bool(
                con.execute(
                    "SELECT 1 FROM messages WHERE id < ? LIMIT 1", (rows[0]["id"],)
                ).fetchone()
            )

        rev = read_state_rev(con)
        # 撤回 / 权限变更的增量：只回传版本号比客户端新的消息状态
        recall_changes = []
        if since_rev > 0 and rev > since_rev:
            recall_changes = [
                recall_state(r)
                for r in con.execute(
                    MSG_SELECT
                    + " WHERE m.recall_rev > ? ORDER BY m.recall_rev ASC LIMIT 200",
                    (since_rev,),
                ).fetchall()
            ]

    viewer = identity_view(nickname)
    admin = viewer["is_admin"]
    messages = [message_dict(r, nickname, admin) for r in rows]
    people = online_users()
    oldest_id = messages[0]["id"] if messages else None
    newest_id = messages[-1]["id"] if messages else None
    # 游标只基于本次真实返回的消息，避免“取完列表后又插入新消息”导致客户端跳过。
    latest_id = newest_id if newest_id is not None else (after_id or 0)
    return jsonify(
        ok=True,
        group=GROUP_NAME,
        messages=messages,
        count=len(messages),
        oldest_id=oldest_id,
        newest_id=newest_id,
        latest_id=latest_id,
        has_more=has_more,
        online_count=len(people),
        online=[p["nickname"] for p in people],
        admins=admin_nicknames(),
        viewer=viewer,
        rev=rev,
        recall_changes=recall_changes,
        server_time=now_iso(),
    )


@app.get("/api/messages/<int:message_id>")
def api_message_detail(message_id: int):
    nickname, error = require_user()
    if error:
        return error
    row = fetch_message(message_id)
    if not row:
        return api_error("not_found", "消息不存在", 404)
    viewer = identity_view(nickname)
    return jsonify(
        ok=True,
        message=message_dict(row, nickname, viewer["is_admin"]),
        viewer=viewer,
    )


@app.post("/api/messages")
def api_post_message():
    payload = json_body()
    nickname, error = require_user(payload)
    if error:
        return error
    ensure_identity(nickname)
    muted = muted_response(nickname)
    if muted is not None:
        return muted

    raw_text = payload.get("text") if "text" in payload else request.form.get("text")
    text, error = normalize_text(raw_text)
    if error:
        return api_error("invalid_text", error, 400)

    # 可选：把桌面上已有的附件引用进线性流（不重复上传文件）
    attachment_id = payload.get("attachment_id")
    kind = "text"
    if attachment_id is not None:
        if isinstance(attachment_id, bool) or not isinstance(attachment_id, int):
            return api_error("invalid_attachment", "attachment_id 必须是整数", 400)
        with db() as con:
            row = con.execute(
                "SELECT mime FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
        if not row:
            return api_error("not_found", "附件不存在", 404)
        kind = "image" if row["mime"] in INLINE_IMAGE_MIMES else "file"

    if not text and attachment_id is None:
        return api_error("empty_message", "消息内容不能为空", 400)
    if rate_limited(nickname):
        resp = api_error("rate_limited", "发送太快了，请稍后再试", 429)
        resp.headers["Retry-After"] = "10"
        return resp

    if attachment_id is None:
        row = insert_text_message(nickname, text)
    else:
        row = insert_attachment_message(nickname, text, attachment_id, kind)
    viewer = identity_view(nickname)
    return jsonify(
        ok=True, message=message_dict(row, nickname, viewer["is_admin"])
    ), 201


@app.post("/api/messages/<int:message_id>/recall")
def api_recall_message(message_id: int):
    """撤回（隐藏）消息：本人可撤回自己的发言，管理员可撤回任何人的发言。"""
    nickname, error = require_user()
    if error:
        return error
    viewer = identity_view(nickname)
    row, code, message = set_recall(message_id, True, nickname, viewer["is_admin"])
    if not row:
        return api_error(code, message, 403 if code == "forbidden" else 404)
    return jsonify(
        ok=True,
        message=message_dict(row, nickname, viewer["is_admin"]),
        recalled=True,
        rev=read_state_rev_now(),
    )


@app.post("/api/messages/<int:message_id>/unrecall")
def api_unrecall_message(message_id: int):
    """撤销撤回：撤回者本人或任意管理员可以把消息重新显示出来。"""
    nickname, error = require_user()
    if error:
        return error
    viewer = identity_view(nickname)
    row, code, message = set_recall(message_id, False, nickname, viewer["is_admin"])
    if not row:
        return api_error(code, message, 403 if code == "forbidden" else 404)
    return jsonify(
        ok=True,
        message=message_dict(row, nickname, viewer["is_admin"]),
        recalled=False,
        rev=read_state_rev_now(),
    )


@app.route("/api/messages/<int:message_id>", methods=["PUT", "PATCH", "DELETE"])
def api_messages_immutable(message_id: int):
    return api_error(
        "append_only",
        "消息内容不可编辑或删除；如需隐藏，请调用 "
        "POST /api/messages/{id}/recall（撤回），"
        "POST /api/messages/{id}/unrecall（撤销撤回）",
        405,
    )


# ---------------------------------------------------------------------------
# 管理员：权限管理
# ---------------------------------------------------------------------------

def iso_in(seconds: int) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_mute(raw) -> tuple[str | None, int, str | None]:
    """解析禁言参数，返回 (到期时间, 是否永久, 错误信息)。"""
    if raw is None:
        return None, 0, None
    if not isinstance(raw, dict):
        return None, 0, "mute 必须是对象或 null"
    if raw.get("forever") is True:
        return None, 1, None
    if raw.get("seconds") is not None:
        try:
            seconds = int(raw["seconds"])
        except (TypeError, ValueError):
            return None, 0, "seconds 必须是整数"
        if seconds <= 0:
            return None, 1, None
        if seconds > 10 * 365 * 86400:
            return None, 0, "禁言时长过长（最多 10 年）"
        return iso_in(seconds), 0, None
    if raw.get("until"):
        text = str(raw["until"]).strip()
        try:
            end = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None, 0, "until 必须是 ISO8601 时间，例如 2026-11-01T00:00:00Z"
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if end <= datetime.now(timezone.utc):
            return None, 0, "禁言结束时间必须晚于当前时间"
        return (
            end.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            0,
            None,
        )
    return None, 0, "mute 需要 seconds / until / forever 之一"


def _require_admin(actor: str):
    if not is_admin(actor):
        return api_error("forbidden", "只有管理员可以进行该操作", 403)
    return None


@app.get("/api/admin/identities")
def api_admin_identities():
    actor, error = require_user()
    if error:
        return error
    denied = _require_admin(actor)
    if denied:
        return denied
    people = identity_roster()
    counts = {
        "total": len(people),
        "admins": sum(1 for p in people if p["is_admin"]),
        "muted": sum(1 for p in people if p["muted"]),
        "locked": sum(1 for p in people if p["locked"]),
        "online": sum(1 for p in people if p["online"]),
    }
    return jsonify(
        ok=True,
        identities=people,
        count=len(people),
        counts=counts,
        admins=admin_nicknames(),
        root=ROOT_NICKNAME,
        mute_presets=MUTE_PRESETS,
        rev=read_state_rev_now(),
    )


@app.post("/api/admin/identities/<nickname>")
def api_admin_update_identity(nickname: str):
    """修改某个昵称的权限：角色 / 禁言 / 锁定。

    请求体示例：
        {"role": "admin"}                     # 设为管理员（"user" 取消）
        {"mute": {"seconds": 600}}            # 禁言 10 分钟
        {"mute": {"until": "2026-11-01T00:00:00Z"}}
        {"mute": {"forever": true}}           # 永久禁言
        {"mute": null}                        # 解除禁言
        {"lock": true} / {"lock": false}      # 锁定 / 解锁昵称
        {"lock": true, "rebind": true}        # 锁定并刷新 IP 快照
    """
    actor, error = require_user()
    if error:
        return error
    denied = _require_admin(actor)
    if denied:
        return denied

    target, nick_error = normalize_nickname(nickname)
    if nick_error:
        return api_error("invalid_nickname", nick_error, 400)
    if target == ROOT_NICKNAME and actor != ROOT_NICKNAME:
        return api_error("forbidden", "只有 root 本人可以修改 root 的权限", 403)

    payload = json_body()
    if not payload:
        return api_error("bad_request", "请求体必须是 JSON 对象", 400)
    if not {"role", "mute", "lock"} & set(payload):
        return api_error(
            "bad_request", "至少要提供 role / mute / lock 之一", 400
        )

    stamp = now_iso()
    ensure_identity(target)

    # ---- 先做完整校验，再统一落库，避免半途失败留下脏状态 ----
    role = None
    if "role" in payload:
        role = str(payload.get("role") or "").strip().lower()
        if role not in ("admin", "user"):
            return api_error("invalid_role", "role 只能是 admin 或 user", 400)
        if target == ROOT_NICKNAME and role != "admin":
            return api_error(
                "root_protected", "root 始终是管理员，不能被降级", 403
            )

    mute_until, mute_forever, mute_error = (None, 0, None)
    if "mute" in payload:
        mute_until, mute_forever, mute_error = parse_mute(payload.get("mute"))
        if mute_error:
            return api_error("invalid_mute", mute_error, 400)
        if mute_error is None and mute_forever == 1 and target == ROOT_NICKNAME:
            return api_error("forbidden", "root 不能被永久禁言", 403)

    lock_value = None
    if "lock" in payload:
        lock_value = bool(payload.get("lock"))
        if lock_value and target == ROOT_NICKNAME and actor != ROOT_NICKNAME:
            return api_error("forbidden", "只有 root 本人可以锁定 root", 403)

    ip = client_ip()
    with db() as con:
        row = _identity_row(con, target)
        if role is not None and row["role"] != role:
            if row["role"] == "admin" and role == "user":
                admins = con.execute(
                    "SELECT COUNT(*) AS c FROM identities WHERE role = 'admin'"
                ).fetchone()["c"]
                if admins <= 1:
                    return api_error(
                        "last_admin",
                        "至少要保留一名管理员，无法取消最后一名管理员",
                        400,
                    )
            con.execute(
                "UPDATE identities SET role = ?, updated_at = ? WHERE nickname = ?",
                (role, stamp, target),
            )

        if "mute" in payload:
            con.execute(
                """
                UPDATE identities
                   SET muted_until = ?, muted_forever = ?,
                       muted_at = ?, muted_by = ?, updated_at = ?
                 WHERE nickname = ?
                """,
                (
                    mute_until,
                    mute_forever,
                    stamp if (mute_until or mute_forever) else None,
                    actor if (mute_until or mute_forever) else None,
                    stamp,
                    target,
                ),
            )

        if lock_value is not None:
            if lock_value:
                con.execute(
                    """
                    UPDATE identities
                       SET locked = 1, locked_at = ?, locked_by = ?, updated_at = ?
                     WHERE nickname = ?
                    """,
                    (stamp, actor, stamp, target),
                )
                if payload.get("rebind"):
                    con.execute("DELETE FROM lock_ips WHERE nickname = ?", (target,))
                seen = [
                    r["ip"]
                    for r in con.execute(
                        "SELECT ip FROM identity_ips WHERE nickname = ?", (target,)
                    ).fetchall()
                ]
                if target == actor and ip:
                    seen.append(ip)
                for bound_ip in set(seen):
                    if not bound_ip or bound_ip in LOCK_IGNORE_IPS:
                        continue
                    con.execute(
                        "INSERT OR IGNORE INTO lock_ips (nickname, ip, bound_at) "
                        "VALUES (?, ?, ?)",
                        (target, bound_ip, stamp),
                    )
            else:
                con.execute(
                    """
                    UPDATE identities
                       SET locked = 0, locked_at = NULL, locked_by = NULL,
                           updated_at = ?
                     WHERE nickname = ?
                    """,
                    (stamp, target),
                )
                con.execute("DELETE FROM lock_ips WHERE nickname = ?", (target,))

    bump_state_rev()
    return jsonify(
        ok=True,
        identity=identity_detail(target),
        rev=read_state_rev_now(),
    )


# ---------------------------------------------------------------------------
# 上传 / 下载
# ---------------------------------------------------------------------------

def store_upload_file(file) -> tuple[dict | None, object | None]:
    """把 multipart 文件落盘并写入 attachments，线性消息与桌面物件共用。

    返回 (附件信息, 错误响应)。附件信息里的 stored_name 只供内部清理使用，
    不会出现在任何 API 响应里。
    """
    display_name = clean_filename(file.filename)
    mime = guess_mime(display_name, file.mimetype)
    ext = safe_ext(display_name)

    temp_path = UPLOAD_DIR / f"{uuid.uuid4().hex}.part"
    final_name = f"{uuid.uuid4().hex}{ext}"
    final_path = UPLOAD_DIR / final_name
    digest = hashlib.sha256()
    size = 0
    try:
        with open(temp_path, "wb") as out:
            while True:
                chunk = file.stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise RequestEntityTooLarge()
                digest.update(chunk)
                out.write(chunk)
        os.replace(temp_path, final_path)
    except RequestEntityTooLarge:
        temp_path.unlink(missing_ok=True)
        return None, api_error(
            "file_too_large", f"单个文件最大 {MAX_UPLOAD_MB} MB", 413
        )
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        app.logger.exception("保存上传文件失败")
        return None, api_error("storage_error", f"保存文件失败：{exc}", 500)

    stamp = now_iso()
    try:
        with db() as con:
            cur = con.execute(
                """
                INSERT INTO attachments
                    (stored_name, original_name, mime, size, sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (final_name, display_name, mime, size, digest.hexdigest(), stamp),
            )
            attachment_id = cur.lastrowid
    except sqlite3.Error:
        final_path.unlink(missing_ok=True)
        app.logger.exception("写入附件记录失败")
        return None, api_error("storage_error", "保存文件记录失败", 500)

    return {
        "id": attachment_id,
        "stored_name": final_name,
        "name": display_name,
        "mime": mime,
        "size": size,
        "size_human": human_size(size),
        "is_image": mime in INLINE_IMAGE_MIMES,
    }, None


def drop_orphan_attachment(info: dict) -> None:
    """物件 / 消息落库失败时清理刚刚保存的附件，避免产生孤儿文件。"""
    try:
        with db() as con:
            con.execute("DELETE FROM attachments WHERE id = ?", (info["id"],))
        (UPLOAD_DIR / info["stored_name"]).unlink(missing_ok=True)
    except (OSError, sqlite3.Error):
        app.logger.exception("清理孤儿附件失败")


@app.post("/api/upload")
def api_upload():
    nickname, error = require_user()
    if error:
        return error
    ensure_identity(nickname)
    muted = muted_response(nickname)
    if muted is not None:
        return muted
    if rate_limited(f"upload:{nickname}", limit=8, window=20.0):
        resp = api_error("rate_limited", "上传太频繁，请稍后再试", 429)
        resp.headers["Retry-After"] = "20"
        return resp

    file = request.files.get("file")
    if file is None or not file.filename:
        return api_error("file_required", "请通过 multipart/form-data 上传 file 字段", 400)

    caption, error = normalize_text(request.form.get("text"))
    if error:
        return api_error("invalid_text", error, 400)

    info, upload_error = store_upload_file(file)
    if upload_error is not None:
        return upload_error
    kind = "image" if info["is_image"] else "file"

    color = color_for(nickname)
    stamp = now_iso()
    try:
        with db() as con:
            cur = con.execute(
                """
                INSERT INTO messages
                    (nickname, color, kind, body, attachment_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (nickname, color, kind, caption or "", info["id"], stamp),
            )
            message_id = cur.lastrowid
    except sqlite3.Error:
        drop_orphan_attachment(info)
        app.logger.exception("写入上传记录失败")
        return api_error("storage_error", "保存文件记录失败", 500)

    notify_new_message()
    row = fetch_message(message_id)
    viewer = identity_view(nickname)
    return jsonify(
        ok=True, message=message_dict(row, nickname, viewer["is_admin"])
    ), 201


@app.get("/api/files/<int:attachment_id>")
def api_file(attachment_id: int):
    _, error = require_user()
    if error:
        return error
    with db() as con:
        row = con.execute(
            "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
    if not row:
        return api_error("not_found", "文件不存在", 404)

    path = UPLOAD_DIR / row["stored_name"]
    if not path.is_file():
        return api_error("not_found", "文件已从磁盘丢失", 404)

    mime = row["mime"] or "application/octet-stream"
    wants_download = request.args.get("download") in ("1", "true", "yes")
    inline = mime in INLINE_IMAGE_MIMES and not wants_download

    resp = send_file(
        path,
        mimetype=mime,
        as_attachment=not inline,
        download_name=row["original_name"],
        conditional=True,
        etag=True,
        last_modified=path.stat().st_mtime,
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Download-Options"] = "noopen"
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    if inline:
        # 即使图片被直接打开，也禁止其执行任何脚本 / 加载外部资源
        resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return resp


# ---------------------------------------------------------------------------
# 共享桌面：二维无限画布
#   * 物件（便签 / 图片 / 文件 / 笔画）是可变的，用 desk_rev 做后写覆盖同步；
#   * 光标 / 视野是瞬时状态，只存进程内存，20 秒无心跳即消失；
#   * 与线性消息共用昵称、禁言、限流、附件与安全策略，两套内容互不覆盖。
# ---------------------------------------------------------------------------

_desk_lock = threading.Lock()          # 保护光标表与 presence 版本号
_desk_cond = threading.Condition()     # 桌面长轮询的唤醒条件
_desk_cursors: dict[str, dict] = {}
_desk_rev_value = 0                   # settings.desk_rev 的内存镜像
_desk_presence_rev = 0
_desk_last_presence_push = 0.0


def init_desk_runtime() -> None:
    """启动时把 desk_rev 内存镜像与数据库对齐（重启后客户端游标仍然有效）。"""
    global _desk_rev_value
    with db() as con:
        row = con.execute(
            "SELECT value FROM settings WHERE key = 'desk_rev'"
        ).fetchone()
        try:
            value = int(row["value"]) if row else 0
        except (TypeError, ValueError):
            value = 0
        top = con.execute(
            "SELECT COALESCE(MAX(rev), 0) AS m FROM desk_items"
        ).fetchone()["m"]
        value = max(value, int(top or 0))
        con.execute(
            """
            INSERT INTO settings (key, value) VALUES ('desk_rev', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(value),),
        )
    _desk_rev_value = value


def desk_rev_now() -> int:
    return _desk_rev_value


def notify_desk() -> None:
    with _desk_cond:
        _desk_cond.notify_all()


def _publish_desk_rev(value: int) -> None:
    global _desk_rev_value
    if value > _desk_rev_value:
        _desk_rev_value = value
    notify_desk()


def _next_desk_rev(con: sqlite3.Connection) -> int:
    """在调用方的事务内推进版本号，返回新值。"""
    con.execute(
        """
        UPDATE settings
           SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
         WHERE key = 'desk_rev'
        """
    )
    row = con.execute(
        "SELECT value FROM settings WHERE key = 'desk_rev'"
    ).fetchone()
    return int(row["value"]) if row else desk_rev_now() + 1


def wait_for_desk(since_rev: int, since_presence: int, timeout: float) -> None:
    """阻塞到物件有变化，或（限频）桌面光标有变化。"""
    global _desk_last_presence_push
    if timeout <= 0:
        return
    deadline = time.monotonic() + min(timeout, 25.0)
    with _desk_cond:
        while True:
            now = time.monotonic()
            if desk_rev_now() > since_rev:
                return
            # 光标心跳很密集，最多每 0.18 秒放行一次，避免打爆所有长轮询
            if (
                _desk_presence_rev > since_presence
                and now - _desk_last_presence_push >= 0.18
            ):
                _desk_last_presence_push = now
                return
            remaining = deadline - now
            if remaining <= 0:
                return
            _desk_cond.wait(min(remaining, 0.2))


def active_desk_cursors() -> list[dict]:
    """仍在有效期内的桌面光标（含调用者自己，客户端按 client_id 过滤）。"""
    now = time.monotonic()
    with _desk_lock:
        for key in [
            key
            for key, value in _desk_cursors.items()
            if now - value["ts"] > DESK_CURSOR_TTL
        ]:
            _desk_cursors.pop(key, None)
        records = [dict(value) for value in _desk_cursors.values()]
    result = []
    for record in records:
        ts = record.pop("ts", now)
        record["idle"] = round(max(0.0, now - ts), 1)
        result.append(record)
    return result


def desk_active_count() -> int:
    return len({cursor["nickname"] for cursor in active_desk_cursors()})


# ---- 输入校验：所有来自客户端的数字 / 颜色 / 笔画都在这里收敛 ----

def clamp_number(
    value,
    default: float = 0.0,
    low: float = -float(DESK_COORD_LIMIT),
    high: float = float(DESK_COORD_LIMIT),
) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(low, min(high, number))


def desk_coord(value, default: float = 0.0) -> float:
    return round(clamp_number(value, default), 2)


def desk_dim(value, default: float = 0.0) -> float:
    return round(clamp_number(value, default, 0.0, DESK_MAX_DIM), 2)


def clean_desk_style(raw) -> dict:
    """只保留白名单字段；颜色必须是 #rrggbb，杜绝 CSS 注入。"""
    if not isinstance(raw, dict):
        raw = {}
    out: dict = {}
    for key in ("color", "note"):
        value = raw.get(key)
        if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            out[key] = value.lower()
    if "width" in raw:
        out["width"] = round(clamp_number(raw.get("width"), 3.0, 0.5, 40.0), 1)
    if "fontSize" in raw:
        # 只接受数字并夹到合理范围，杜绝往 style 里塞任意 CSS
        out["fontSize"] = round(
            clamp_number(
                raw.get("fontSize"),
                DESK_NOTE_FONT_DEFAULT,
                DESK_NOTE_FONT_MIN,
                DESK_NOTE_FONT_MAX,
            ),
            1,
        )
    source = raw.get("source")
    if isinstance(source, dict):
        message_id = source.get("message_id")
        out["source"] = {
            "from": str(source.get("from") or "")[:16],
            "author": str(source.get("author") or "")[:MAX_NICK],
            "message_id": (
                message_id
                if isinstance(message_id, int) and not isinstance(message_id, bool)
                else None
            ),
        }
    return out


def normalize_desk_text(raw) -> tuple[str | None, str | None]:
    if raw is None:
        return "", None
    if not isinstance(raw, str):
        return None, "文字格式不正确"
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    if len(text) > DESK_MAX_TEXT:
        return None, f"桌面文字最多 {DESK_MAX_TEXT} 个字符"
    return text, None


def normalize_stroke(raw) -> tuple[str | None, float, float, str | None]:
    """笔画 body 是相对坐标点数组，重新紧凑序列化并算出包围盒。"""
    if not isinstance(raw, str) or not raw or len(raw) > DESK_MAX_BODY:
        return None, 0.0, 0.0, "笔画数据为空或过大"
    try:
        points = json.loads(raw)
    except (TypeError, ValueError):
        return None, 0.0, 0.0, "笔画数据不是合法 JSON"
    if not isinstance(points, list) or not points:
        return None, 0.0, 0.0, "笔画至少需要一个点"
    if len(points) > DESK_MAX_POINTS:
        return None, 0.0, 0.0, f"单笔最多 {DESK_MAX_POINTS} 个点"
    clean = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None, 0.0, 0.0, "笔画点格式不正确"
        for value in point:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None, 0.0, 0.0, "笔画坐标必须是数字"
            if not math.isfinite(float(value)):
                return None, 0.0, 0.0, "笔画坐标必须是有限数字"
        clean.append([desk_coord(point[0]), desk_coord(point[1])])
    xs = [point[0] for point in clean]
    ys = [point[1] for point in clean]
    return (
        json.dumps(clean, separators=(",", ":")),
        round(max(xs) - min(xs), 2),
        round(max(ys) - min(ys), 2),
        None,
    )


def desk_item_dict(row) -> dict:
    """物件对外表示；已删除的物件只保留 id / rev / deleted。"""
    hidden = bool(row["deleted"])
    attachment = None
    if not hidden and row["attachment_id"] and row["att_name"]:
        mime = row["att_mime"] or "application/octet-stream"
        attachment = {
            "id": row["attachment_id"],
            "name": row["att_name"],
            "mime": mime,
            "size": row["att_size"] or 0,
            "size_human": human_size(row["att_size"] or 0),
            "is_image": mime in INLINE_IMAGE_MIMES,
            "url": f"/api/files/{row['attachment_id']}",
            "download_url": f"/api/files/{row['attachment_id']}?download=1",
        }
    try:
        style = json.loads(row["style"] or "{}")
    except (TypeError, ValueError):
        style = {}
    if not isinstance(style, dict):
        style = {}
    return {
        "id": row["id"],
        "kind": row["kind"],
        "nickname": row["nickname"],
        "color": row["color"],
        "x": row["x"],
        "y": row["y"],
        "w": row["w"],
        "h": row["h"],
        "z": row["z"] or row["id"],
        "body": "" if hidden else row["body"],
        "style": {} if hidden else style,
        "attachment": attachment,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
        "rev": row["rev"],
        "deleted": hidden,
    }


def fetch_desk_item(item_id: int):
    with db() as con:
        return con.execute(DESK_SELECT + " WHERE i.id = ?", (item_id,)).fetchone()


def desk_int_arg(name: str, default: int):
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return _BAD_INT


def desk_payload() -> dict:
    """创建物件支持纯 JSON，也支持 multipart 里塞一个 meta JSON 字段。"""
    if "meta" in request.form:
        try:
            value = json.loads(request.form.get("meta") or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}
    return json_body()


# ---- 读取：快照 + 增量长轮询 ----

@app.get("/api/desk/snapshot")
def api_desk_snapshot():
    _, error = require_user()
    if error:
        return error
    before_id = desk_int_arg("before_id", 2**62)
    limit = desk_int_arg("limit", 400)
    if _BAD_INT in (before_id, limit):
        return api_error("bad_request", "before_id / limit 必须是整数", 400)
    before_id = max(0, before_id)
    limit = max(1, min(limit, 500))

    with db() as con:
        rows = con.execute(
            DESK_SELECT
            + " WHERE i.deleted = 0 AND i.id < ? ORDER BY i.id DESC LIMIT ?",
            (before_id, limit),
        ).fetchall()
        earliest = rows[-1]["id"] if rows else 0
        has_more = len(rows) == limit and bool(
            con.execute(
                "SELECT 1 FROM desk_items WHERE deleted = 0 AND id < ? LIMIT 1",
                (earliest,),
            ).fetchone()
        )
    return jsonify(
        ok=True,
        items=[desk_item_dict(row) for row in reversed(rows)],
        count=len(rows),
        has_more=has_more,
        rev=desk_rev_now(),
        presence_rev=_desk_presence_rev,
        presence=active_desk_cursors(),
        server_time=now_iso(),
    )


@app.get("/api/desk/events")
def api_desk_events():
    """桌面增量：物件变化 + 在线光标，同时用于 25 秒长轮询。"""
    _, error = require_user()
    if error:
        return error
    since_rev = desk_int_arg("since_rev", 0)
    since_presence = desk_int_arg("since_presence", 0)
    wait = desk_int_arg("wait", 0)
    limit = desk_int_arg("limit", 300)
    if _BAD_INT in (since_rev, since_presence, wait, limit):
        return api_error(
            "bad_request",
            "since_rev / since_presence / wait / limit 必须是整数",
            400,
        )
    since_rev = max(0, since_rev)
    since_presence = max(0, since_presence)
    # 光标版本号只在内存里，服务重启后归零；客户端拿着旧值会一直等不到唤醒
    if since_presence > _desk_presence_rev:
        since_presence = 0
    wait = max(0, min(wait, 25))
    limit = max(1, min(limit, 500))

    wait_for_desk(since_rev, since_presence, wait)
    with db() as con:
        rows = con.execute(
            DESK_SELECT + " WHERE i.rev > ? ORDER BY i.rev ASC LIMIT ?",
            (since_rev, limit),
        ).fetchall()
    return jsonify(
        ok=True,
        items=[desk_item_dict(row) for row in rows],
        count=len(rows),
        has_more=len(rows) == limit,
        rev=desk_rev_now(),
        presence_rev=_desk_presence_rev,
        presence=active_desk_cursors(),
        server_time=now_iso(),
    )


# ---- 写入：放置 / 移动 / 编辑 / 删除 / 恢复 ----

@app.post("/api/desk/items")
def api_desk_create_item():
    payload = desk_payload()
    nickname, error = require_user(payload)
    if error:
        return error
    ensure_identity(nickname)
    muted = muted_response(nickname)
    if muted is not None:
        return muted
    if rate_limited(f"desk:create:{nickname}", limit=40, window=10.0):
        resp = api_error("rate_limited", "放置太快了，请稍后再试", 429)
        resp.headers["Retry-After"] = "10"
        return resp

    kind = payload.get("kind")
    if kind not in DESK_KINDS:
        return api_error(
            "invalid_kind", "物件类型必须是 text / image / file / stroke", 400
        )

    x = desk_coord(payload.get("x"))
    y = desk_coord(payload.get("y"))
    w = desk_dim(payload.get("w"))
    h = desk_dim(payload.get("h"))
    style = clean_desk_style(payload.get("style"))
    body = ""
    attachment_id = None
    upload_info = None

    file = request.files.get("file")
    if file is not None and file.filename:
        if kind in ("text", "stroke"):
            return api_error("invalid_kind", "文字 / 笔画物件不能带文件", 400)
        upload_info, upload_error = store_upload_file(file)
        if upload_error is not None:
            return upload_error
        attachment_id = upload_info["id"]
        kind = "image" if upload_info["is_image"] else "file"

    if kind in ("image", "file"):
        text, text_error = normalize_desk_text(payload.get("body"))
        if text_error:
            return api_error("invalid_text", text_error, 400)
        body = text
        if attachment_id is None:
            raw_id = payload.get("attachment_id")
            if isinstance(raw_id, bool) or not isinstance(raw_id, int):
                return api_error(
                    "invalid_attachment", "attachment_id 必须是整数", 400
                )
            with db() as con:
                row = con.execute(
                    "SELECT mime FROM attachments WHERE id = ?", (raw_id,)
                ).fetchone()
            if not row:
                return api_error("not_found", "附件不存在", 404)
            attachment_id = raw_id
            kind = "image" if row["mime"] in INLINE_IMAGE_MIMES else "file"
        if not w:
            w = 320.0 if kind == "image" else 260.0
        if not h:
            h = 220.0 if kind == "image" else 96.0
    elif kind == "text":
        text, text_error = normalize_desk_text(payload.get("body"))
        if text_error:
            return api_error("invalid_text", text_error, 400)
        if not text.strip():
            return api_error("empty_item", "便签内容不能为空", 400)
        body = text
        w = w or 260.0
        h = 0.0  # 便签高度按内容自适应，不写入数据库
    else:  # stroke
        normalized, box_w, box_h, stroke_error = normalize_stroke(
            payload.get("body")
        )
        if stroke_error:
            return api_error("invalid_stroke", stroke_error, 400)
        body = normalized
        w, h = box_w, box_h
        style.setdefault("color", "#f5b544")
        style.setdefault("width", 3.0)

    stamp = now_iso()
    try:
        with db() as con:
            rev = _next_desk_rev(con)
            cur = con.execute(
                """
                INSERT INTO desk_items
                    (kind, nickname, color, x, y, w, h, z, body, style,
                     attachment_id, created_at, updated_at, updated_by, rev)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind, nickname, color_for(nickname), x, y, w, h, body,
                    json.dumps(style, ensure_ascii=False, separators=(",", ":")),
                    attachment_id, stamp, stamp, nickname, rev,
                ),
            )
            item_id = cur.lastrowid
    except sqlite3.Error:
        if upload_info:
            drop_orphan_attachment(upload_info)
        app.logger.exception("写入桌面物件失败")
        return api_error("storage_error", "保存桌面物件失败", 500)

    _publish_desk_rev(rev)
    return jsonify(
        ok=True, item=desk_item_dict(fetch_desk_item(item_id)), rev=rev
    ), 201


@app.post("/api/desk/items/batch")
def api_desk_batch_items():
    """多选 / 框选后的批量移动、删除或文字字号：一次请求、一个版本号、一次唤醒。

    move      —— 位置人人可改，与单个 PATCH 的规则一致；
    delete    —— 只允许作者或管理员，任一物件越权则整批拒绝；
    font_size —— 只作用于文字便签，同样限作者或管理员，非文字物件自动跳过。
    """
    payload = json_body()
    nickname, error = require_user(payload)
    if error:
        return error
    muted = muted_response(nickname)
    if muted is not None:
        return muted
    if rate_limited(f"desk:batch:{nickname}", limit=60, window=10.0):
        resp = api_error("rate_limited", "操作太快了，请稍后再试", 429)
        resp.headers["Retry-After"] = "10"
        return resp

    action = payload.get("action")
    entries = payload.get("items")
    if action not in ("move", "delete", "font_size"):
        return api_error("invalid_action", "action 只能是 move、delete 或 font_size", 400)
    if not isinstance(entries, list) or not 1 <= len(entries) <= DESK_BATCH_MAX:
        return api_error(
            "invalid_items", f"items 需为 1~{DESK_BATCH_MAX} 个物件的数组", 400
        )
    font_size = None
    if action == "font_size":
        font_size = clean_desk_style({"fontSize": payload.get("fontSize")})["fontSize"]

    by_id: dict = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return api_error("invalid_items", "每个物件必须是对象", 400)
        item_id = entry.get("id")
        if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0:
            return api_error("invalid_items", "物件 id 必须是正整数", 400)
        by_id.setdefault(item_id, entry)
    ids = list(by_id)

    placeholders = ", ".join("?" for _ in ids)
    with db() as con:
        rows = con.execute(
            f"SELECT * FROM desk_items WHERE id IN ({placeholders}) AND deleted = 0",
            ids,
        ).fetchall()
    found = {row["id"]: row for row in rows}

    admin = is_admin(nickname)
    if action in ("delete", "font_size"):
        forbidden = [
            item_id
            for item_id in ids
            if item_id in found
            and not admin
            and found[item_id]["nickname"] != nickname
            # 批量改字号时非文字物件会被跳过，不必因它整批拒绝
            and (action == "delete" or found[item_id]["kind"] == "text")
        ]
        if forbidden:
            return api_error(
                "forbidden",
                "没有权限修改物件 " + ", ".join(str(i) for i in forbidden[:5]),
                403,
            )

    stamp = now_iso()
    changed: list[int] = []
    try:
        with db() as con:
            rev = _next_desk_rev(con)
            for item_id in ids:
                row = found.get(item_id)
                if not row:
                    continue
                if action == "move":
                    x = desk_coord(by_id[item_id].get("x"), row["x"])
                    y = desk_coord(by_id[item_id].get("y"), row["y"])
                    con.execute(
                        "UPDATE desk_items SET x = ?, y = ?, rev = ?, "
                        "updated_at = ?, updated_by = ? WHERE id = ?",
                        (x, y, rev, stamp, nickname, item_id),
                    )
                elif action == "delete":
                    con.execute(
                        "UPDATE desk_items SET deleted = 1, rev = ?, "
                        "updated_at = ?, updated_by = ? WHERE id = ?",
                        (rev, stamp, nickname, item_id),
                    )
                else:  # font_size：合并进原有 style，保留颜色 / 来源等信息
                    if row["kind"] != "text":
                        continue
                    try:
                        style = json.loads(row["style"] or "{}")
                    except (TypeError, ValueError):
                        style = {}
                    if not isinstance(style, dict):
                        style = {}
                    style["fontSize"] = font_size
                    con.execute(
                        "UPDATE desk_items SET style = ?, rev = ?, "
                        "updated_at = ?, updated_by = ? WHERE id = ?",
                        (
                            json.dumps(
                                style, ensure_ascii=False, separators=(",", ":")
                            ),
                            rev,
                            stamp,
                            nickname,
                            item_id,
                        ),
                    )
                changed.append(item_id)
    except sqlite3.Error:
        app.logger.exception("批量更新桌面物件失败")
        return api_error("storage_error", "批量更新桌面物件失败", 500)

    _publish_desk_rev(rev)
    return jsonify(
        ok=True,
        action=action,
        changed=len(changed),
        items=[desk_item_dict(fetch_desk_item(i)) for i in changed],
        font_size=font_size,
        rev=rev,
    )


@app.patch("/api/desk/items/<int:item_id>")
def api_desk_update_item(item_id: int):
    """移动 / 缩放人人可做；改内容、层级、删除限作者或管理员。"""
    nickname, error = require_user()
    if error:
        return error
    muted = muted_response(nickname)
    if muted is not None:
        return muted
    if rate_limited(f"desk:edit:{nickname}", limit=120, window=10.0):
        resp = api_error("rate_limited", "操作太快了，请稍后再试", 429)
        resp.headers["Retry-After"] = "10"
        return resp

    row = fetch_desk_item(item_id)
    if not row or row["deleted"]:
        return api_error("not_found", "物件不存在或已被删除", 404)
    payload = json_body()
    privileged = row["nickname"] == nickname or is_admin(nickname)
    if not privileged and any(key in payload for key in ("body", "style", "z")):
        return api_error(
            "forbidden", "只能移动 / 缩放他人放置的物件，编辑请找作者或管理员", 403
        )

    updates: dict = {}
    for key in ("x", "y"):
        if key in payload:
            updates[key] = desk_coord(payload.get(key), row[key])
    for key in ("w", "h"):
        if key in payload:
            updates[key] = desk_dim(payload.get(key), row[key])
    if "z" in payload:
        updates["z"] = int(clamp_number(payload.get("z"), row["z"], -1e9, 1e9))
    if "style" in payload:
        updates["style"] = json.dumps(
            clean_desk_style(payload.get("style")),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if "body" in payload:
        if row["kind"] == "stroke":
            normalized, box_w, box_h, stroke_error = normalize_stroke(
                payload.get("body")
            )
            if stroke_error:
                return api_error("invalid_stroke", stroke_error, 400)
            updates["body"] = normalized
            updates["w"], updates["h"] = box_w, box_h
        else:
            text, text_error = normalize_desk_text(payload.get("body"))
            if text_error:
                return api_error("invalid_text", text_error, 400)
            updates["body"] = text

    if not updates:
        return jsonify(ok=True, item=desk_item_dict(row), rev=desk_rev_now())

    assignments = ", ".join(f"{key} = ?" for key in updates)
    try:
        with db() as con:
            rev = _next_desk_rev(con)
            con.execute(
                f"UPDATE desk_items SET {assignments}, "
                "rev = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                (*updates.values(), rev, now_iso(), nickname, item_id),
            )
    except sqlite3.Error:
        app.logger.exception("更新桌面物件失败")
        return api_error("storage_error", "更新桌面物件失败", 500)
    _publish_desk_rev(rev)
    return jsonify(
        ok=True, item=desk_item_dict(fetch_desk_item(item_id)), rev=rev
    )


def _set_desk_deleted(item_id: int, deleted: bool, actor: str, admin: bool):
    row = fetch_desk_item(item_id)
    if not row:
        return None, "not_found", "物件不存在"
    if not admin and row["nickname"] != actor:
        return None, "forbidden", "只能删除自己放置的物件"
    if bool(row["deleted"]) == deleted:
        return row, "", ""
    with db() as con:
        rev = _next_desk_rev(con)
        con.execute(
            """
            UPDATE desk_items
               SET deleted = ?, rev = ?, updated_at = ?, updated_by = ?
             WHERE id = ?
            """,
            (1 if deleted else 0, rev, now_iso(), actor, item_id),
        )
    _publish_desk_rev(rev)
    return fetch_desk_item(item_id), "", ""


@app.delete("/api/desk/items/<int:item_id>")
def api_desk_delete_item(item_id: int):
    nickname, error = require_user()
    if error:
        return error
    muted = muted_response(nickname)
    if muted is not None:
        return muted
    viewer = identity_view(nickname)
    row, code, message = _set_desk_deleted(
        item_id, True, nickname, viewer["is_admin"]
    )
    if not row:
        return api_error(code, message, 403 if code == "forbidden" else 404)
    return jsonify(ok=True, item=desk_item_dict(row), rev=desk_rev_now())


@app.post("/api/desk/items/<int:item_id>/restore")
def api_desk_restore_item(item_id: int):
    nickname, error = require_user()
    if error:
        return error
    muted = muted_response(nickname)
    if muted is not None:
        return muted
    viewer = identity_view(nickname)
    row, code, message = _set_desk_deleted(
        item_id, False, nickname, viewer["is_admin"]
    )
    if not row:
        return api_error(code, message, 403 if code == "forbidden" else 404)
    return jsonify(ok=True, item=desk_item_dict(row), rev=desk_rev_now())


# ---- 在线位置：光标心跳 / 离开 ----

@app.post("/api/desk/cursor")
def api_desk_cursor():
    """上报鼠标世界坐标、相机中心与视野，供其他人的桌面渲染光标。"""
    global _desk_presence_rev
    nickname, error = require_user()
    if error:
        return error
    payload = json_body()
    client_id = payload.get("client_id")
    if not isinstance(client_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{4,64}", client_id
    ):
        return api_error("invalid_client", "client_id 格式不正确", 400)

    x = desk_coord(payload.get("x"))
    y = desk_coord(payload.get("y"))
    cx = desk_coord(payload.get("cx"), x)
    cy = desk_coord(payload.get("cy"), y)
    zoom = clamp_number(payload.get("zoom"), 1.0, 0.05, 20.0)
    vw = clamp_number(payload.get("vw"), 1000.0, 1.0, 1e6)
    vh = clamp_number(payload.get("vh"), 700.0, 1.0, 1e6)
    tool = payload.get("tool")
    tool = tool if isinstance(tool, str) and re.fullmatch(r"[a-z]{0,16}", tool) else ""

    now = time.monotonic()
    record = {
        "client_id": client_id,
        "nickname": nickname,
        "color": color_for(nickname),
        "x": x,
        "y": y,
        "cx": cx,
        "cy": cy,
        "zoom": round(zoom, 3),
        "viewport": {
            "x": round(cx - vw / 2, 2),
            "y": round(cy - vh / 2, 2),
            "w": round(vw, 2),
            "h": round(vh, 2),
        },
        "tool": tool,
        "ts": now,
    }
    with _desk_lock:
        for key in [
            key
            for key, value in _desk_cursors.items()
            if now - value["ts"] > DESK_CURSOR_TTL
        ]:
            _desk_cursors.pop(key, None)
        _desk_cursors[client_id] = record
        _desk_presence_rev += 1
        prev = _desk_presence_rev
    notify_desk()
    return jsonify(ok=True, presence_rev=prev, rev=desk_rev_now())


@app.post("/api/desk/leave")
def api_desk_leave():
    """切回线性视图 / 关闭页面时立刻撤下自己的光标。"""
    global _desk_presence_rev
    _, error = require_user()
    if error:
        return error
    client_id = json_body().get("client_id")
    with _desk_lock:
        if isinstance(client_id, str):
            _desk_cursors.pop(client_id, None)
        _desk_presence_rev += 1
        prev = _desk_presence_rev
    notify_desk()
    return jsonify(ok=True, presence_rev=prev)


# ---------------------------------------------------------------------------
# 健康检查 / 错误处理 / 安全响应头
# ---------------------------------------------------------------------------

@app.get("/api/health")
def api_health():
    try:
        with db() as con:
            count = con.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
            desk_count = con.execute(
                "SELECT COUNT(*) AS c FROM desk_items WHERE deleted = 0"
            ).fetchone()["c"]
            versions = [
                r["version"]
                for r in con.execute("SELECT version FROM schema_version").fetchall()
            ]
        db_ok = True
    except sqlite3.Error:
        count = -1
        desk_count = -1
        versions = []
        db_ok = False
    return jsonify(
        ok=True,
        app=APP_NAME,
        version=APP_VERSION,
        status="ok" if db_ok else "degraded",
        group=GROUP_NAME,
        message_count=count,
        desk_item_count=desk_count,
        desk_active=desk_active_count(),
        db=db_ok,
        schema=versions,
        max_upload_mb=MAX_UPLOAD_MB,
        server_time=now_iso(),
    )


@app.errorhandler(HTTPException)
def handle_http_error(exc: HTTPException):
    if request.path.startswith("/api/"):
        default_messages = {
            400: "请求格式不正确",
            401: "需要昵称或登录状态",
            403: "没有权限",
            404: "接口或资源不存在",
            405: "该请求方法不被支持",
            413: f"上传内容过大，单个文件最大 {MAX_UPLOAD_MB} MB",
            415: "不支持的请求内容类型",
            429: "请求过于频繁，请稍后再试",
        }
        message = default_messages.get(exc.code, exc.description or exc.name)
        if isinstance(exc, RequestEntityTooLarge):
            message = f"上传内容过大，单个文件最大 {MAX_UPLOAD_MB} MB"
        return api_error(exc.name.lower().replace(" ", "_"), message, exc.code or 500)
    return exc.get_response()


@app.errorhandler(Exception)
def handle_unexpected_error(exc: Exception):
    if isinstance(exc, HTTPException):
        return handle_http_error(exc)
    app.logger.exception("未处理异常: %s", exc)
    if request.path.startswith("/api/"):
        return api_error("internal_error", "服务器内部错误", 500)
    return "Internal Server Error", 500


@app.after_request
def add_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith("/api/"):
        resp.headers.setdefault("Access-Control-Allow-Origin", "*")
        resp.headers.setdefault(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Nickname, X-Chat-Client, Range",
        )
        resp.headers.setdefault(
            "Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS"
        )
        if not request.path.startswith("/api/files/"):
            resp.headers.setdefault("Cache-Control", "no-store")
    if request.path == "/":
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' blob: data:; "
            "media-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; "
            "connect-src 'self'; "
            "base-uri 'none'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        resp.headers["X-Frame-Options"] = "DENY"
    return resp


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def local_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def print_banner() -> None:
    print("=" * 58)
    print(f"  {APP_NAME} v{APP_VERSION} · 单群聊 + 共享桌面 · 端口 {PORT}")
    print("=" * 58)
    print(f"  本机访问 : http://127.0.0.1:{PORT}")
    if HOST in ("0.0.0.0", "::"):
        print(f"  局域网   : http://{local_ip()}:{PORT}")
    print(f"  共享桌面 : http://127.0.0.1:{PORT}/#desk")
    print(f"  数据目录 : {DATA_DIR}")
    print(f"  单文件上限: {MAX_UPLOAD_MB} MB")
    print("  按 Ctrl+C 停止服务")
    print("=" * 58)


init_storage()
init_desk_runtime()


if __name__ == "__main__":
    print_banner()
    app.run(
        host=HOST,
        port=PORT,
        threaded=True,
        debug=False,
        use_reloader=False,
    )
