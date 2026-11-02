# Chat5050 API 文档

Chat5050 的 Web 前端与 headless 客户端使用同一套 HTTP API。所有接口返回 UTF-8 JSON；上传接口使用 `multipart/form-data`。

- **Base URL**：`http://127.0.0.1:5050`
- **时间格式**：UTC ISO 8601，例如 `2026-10-18T08:30:00Z`
- **消息模型**：追加式，不支持编辑 / 删除；支持“撤回”（隐藏，可撤销）。
- **身份模型**：昵称即身份，没有密码或 token。昵称有三个维度：管理员 / 普通用户、禁言 / 未禁言、锁定 / 未锁定。
- **权限模型**：初始只有 `root` 是管理员且默认锁定；管理员可以管理所有人的权限。详见 [16. 权限管理](#16-权限管理管理员)。
- **锁定只约束 Web 前端**：headless 调用带 `X-Chat-Client: api` 时仍然可以自由使用任意昵称。

---

## 1. 身份（昵称）

除公开接口（`/api/health`、`/api/group`、`/api/login`、`/api/logout`）外，其余接口都需要昵称。

### 传递方式（按优先级）

| 优先级 | 方式 | 示例 |
| --- | --- | --- |
| 1 | URL 查询参数 | `?nickname=alice` |
| 2 | JSON 请求体 | `{"nickname":"alice","text":"hi"}` |
| 3 | 表单字段 | `nickname=alice` |
| 4 | 请求头 | `X-Nickname: alice` |
| 5 | Web session | 先调用 `POST /api/login` |

> URL 参数和 JSON 字段最省事，中文直接使用即可。
>
> HTTP 头只能承载 latin-1，中文昵称需要将 UTF-8 字节按 latin-1 编码后放入 `X-Nickname`。例如 Python：
> `"小明".encode("utf-8").decode("latin-1")`。不熟悉时请改用查询参数。

昵称规则：

- 先做 Unicode NFKC 规范化，合并连续空白；
- 去掉控制字符与双向控制符；
- 长度 1–24 个字符；
- 区分大小写，不做唯一性校验。

### 昵称的三个状态

| 维度 | 取值 | 作用 |
| --- | --- | --- |
| 角色 | `admin` / `user` | 管理员可以管理所有人的权限、撤回任何人的消息 |
| 禁言 | 未禁言 / 禁言至某个时间 / 永久禁言 | 禁言期间不能发送消息、不能上传（Web 与 API 都生效） |
| 锁定 | 未锁定 / 已锁定 | 只有 Web 前端受限：其他人不能改成这个昵称，该昵称绑定过的 IP 也不能换昵称 |

初始状态：只有 `root` 是管理员，并且处于锁定状态。管理员可以在网页右上角的「管理」面板里操作，也可以直接调用 [管理员接口](#161-权限管理接口)。

> 锁定是“前端防冒用”，不是安全认证：headless 客户端带上 `X-Chat-Client: api`（或登录请求体带 `"api": true`）即可自由使用任意昵称。

### 可选的 Web 登录

`POST /api/login` 会把昵称写入 Flask session cookie，方便浏览器端使用。Headless 客户端**不必**登录，每次请求带上昵称即可。

```bash
curl -c cookies.txt -X POST http://127.0.0.1:5050/api/login \
  -H "Content-Type: application/json" \
  -d '{"nickname":"alice"}'

curl -b cookies.txt "http://127.0.0.1:5050/api/messages?limit=10"
```

---

## 2. 数据结构

### Message

```json
{
  "id": 12,
  "nickname": "小明",
  "color": "#5edeb8",
  "kind": "image",
  "text": "看这张图",
  "created_at": "2026-10-18T08:30:00Z",
  "attachment": {
    "id": 4,
    "name": "测试图片.png",
    "mime": "image/png",
    "size": 51234,
    "size_human": "50.0 KB",
    "is_image": true,
    "url": "/api/files/4",
    "download_url": "/api/files/4?download=1"
  },
  "recalled": false,
  "recalled_by": null,
  "recalled_at": null,
  "recall_rev": 0,
  "own": false,
  "can_recall": true,
  "can_unrecall": false
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | integer | 自增消息 id，也是排序与游标依据 |
| `nickname` | string | 发送者昵称 |
| `color` | string | 由昵称稳定生成的 `#rrggbb` 颜色 |
| `kind` | string | `text` / `image` / `file` |
| `text` | string | 文字内容；文件消息可为空或为说明文字；**已撤回时为空字符串** |
| `created_at` | string | UTC ISO8601 |
| `attachment` | object \| null | 附件信息，文字消息或已撤回消息为 `null` |
| `recalled` | boolean | 是否已被撤回（隐藏） |
| `recalled_by` | string \| null | 执行撤回的昵称 |
| `recalled_at` | string \| null | 撤回时间 |
| `recall_rev` | integer | 撤回状态版本号，用于增量同步 |
| `own` | boolean | 是否是当前请求者自己的消息 |
| `can_recall` | boolean | 当前请求者能否撤回这条消息 |
| `can_unrecall` | boolean | 当前请求者能否撤销这条消息的撤回 |

> 撤回的消息只返回“已被撤回”这一事实，正文与附件不会下发；撤销撤回后重新可读。

### Attachment

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | integer | 附件 id |
| `name` | string | 原始文件名 |
| `mime` | string | 服务端判定的 MIME |
| `size` | integer | 字节数 |
| `size_human` | string | 人类可读大小 |
| `is_image` | boolean | 是否允许浏览器内联展示的图片 |
| `url` | string | 相对地址；图片可内联显示，其他类型会强制下载 |
| `download_url` | string | 相对地址；始终以附件形式下载 |

`url` / `download_url` 是**相对路径**，客户端需拼接 Base URL。它们同样需要昵称：浏览器会带 session cookie；脚本请在后面追加 `?nickname=...` 或 `&nickname=...`。

---

## 3. 接口一览

| 方法 | 路径 | 说明 | 需要昵称 |
| --- | --- | --- | --- |
| GET | `/api/health` | 健康检查 | 否 |
| GET | `/api/group` | 群信息 / 统计 | 否 |
| POST | `/api/login` | 设置昵称并创建 session | 否（请求中提供） |
| POST | `/api/logout` | 清除 session | 否 |
| GET | `/api/me` | 当前昵称与群信息 | 是 |
| GET | `/api/presence` | 在线成员 | 是 |
| GET | `/api/messages` | 消息列表 / 历史 / 长轮询 | 是 |
| GET | `/api/messages/{id}` | 单条消息 | 是 |
| POST | `/api/messages` | 发送文字 | 是 |
| POST | `/api/messages/{id}/recall` | 撤回消息（本人或管理员） | 是 |
| POST | `/api/messages/{id}/unrecall` | 撤销撤回 | 是 |
| POST | `/api/upload` | 上传图片 / 文件 | 是 |
| GET | `/api/files/{id}` | 查看 / 下载附件 | 是 |
| GET | `/api/admin/identities` | 权限名单（仅管理员） | 是 |
| POST | `/api/admin/identities/{nickname}` | 修改角色 / 禁言 / 锁定（仅管理员） | 是 |
| PUT/PATCH/DELETE | `/api/messages/{id}` | 始终返回 405（只追加） | — |

---

## 4. 公开接口

### GET /api/health

```bash
curl http://127.0.0.1:5050/api/health
```

```json
{
  "ok": true,
  "app": "Chat5050",
  "version": "1.0.0",
  "status": "ok",
  "group": "大厅",
  "message_count": 42,
  "db": true,
  "schema": ["1"],
  "max_upload_mb": 50,
  "server_time": "2026-10-18T08:30:00Z"
}
```

### GET /api/group

```json
{
  "ok": true,
  "group": {
    "name": "大厅",
    "message_count": 42,
    "attachment_count": 7,
    "online_count": 3,
    "max_upload_mb": 50,
    "max_text": 4000,
    "max_nickname": 24
  }
}
```

---

## 5. 登录 / 当前身份

### POST /api/login

创建或刷新 Web session。请求体可以是 JSON、表单或查询参数。

```bash
curl -X POST http://127.0.0.1:5050/api/login \
  -H "Content-Type: application/json" \
  -d '{"nickname":"alice"}'
```

```json
{
  "ok": true,
  "nickname": "alice",
  "color": "#5b8cff",
  "viewer": {
    "nickname": "alice",
    "role": "user",
    "is_admin": false,
    "muted": false,
    "muted_until": null,
    "muted_forever": false,
    "mute_remaining": null,
    "locked": false
  },
  "group": {
    "name": "大厅",
    "message_count": 42,
    "recalled_count": 1,
    "attachment_count": 7,
    "online_count": 3,
    "admin_count": 1,
    "max_upload_mb": 50,
    "max_text": 4000,
    "max_nickname": 24,
    "mute_presets": [{"label": "10 分钟", "seconds": 600}]
  }
}
```

响应会设置 `chat5050` session cookie（HttpOnly、SameSite=Lax、30 天）。

> 网页登录会做昵称锁定校验，可能返回 `403 nickname_locked` / `403 ip_locked`。
> Headless 客户端如果确实要使用被锁定的昵称，请加请求头 `X-Chat-Client: api`（或在 JSON 里写 `"api": true`）。

### POST /api/logout

```json
{"ok": true}
```

### GET /api/me

```bash
curl "http://127.0.0.1:5050/api/me?nickname=alice"
```

返回与 `/api/login` 相同结构，不创建 session。

---

## 6. 在线成员

### GET /api/presence

```bash
curl "http://127.0.0.1:5050/api/presence?nickname=alice"
```

```json
{
  "ok": true,
  "online_count": 2,
  "online": [
    {"nickname": "alice", "color": "#5b8cff", "last_seen": "2026-10-18T08:30:00Z"},
    {"nickname": "小明", "color": "#5edeb8", "last_seen": "2026-10-18T08:29:51Z"}
  ]
}
```

75 秒内有请求的昵称视为在线。同名昵称只计一次。

---

## 7. 消息列表（核心）

### GET /api/messages

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `nickname` | string | — | 必填（或使用其它身份方式） |
| `limit` | int | 50 | 1–200 |
| `after_id` | int | — | 只返回 `id > after_id` 的消息，升序 |
| `before_id` | int | — | 只返回 `id < before_id` 的消息，升序 |
| `since` | int | — | `after_id` 的别名 |
| `wait` | int | 0 | 0–25；配合 `after_id` 做长轮询 |
| `since_rev` | int | 0 | 上次拿到的 `rev`，用于获取撤回增量 |

行为：

- 不带游标：返回**最新** `limit` 条，按 id 升序；`has_more` 表示是否还有更早消息。
- `before_id`：向前翻历史，按 id 升序；`has_more` 表示是否还有更早消息。
- `after_id`：向后取新消息，按 id 升序；`has_more` 恒为 `false`，如果 `count == limit`，客户端应立即再请求一次。
- `after_id + wait`：服务端最多等待 `wait` 秒，期间出现新消息**或撤回 / 权限变更**立即返回；超时返回空数组。
- `latest_id` 是**本次响应可安全续传的游标**：有消息时等于 `newest_id`；空响应时等于传入的 `after_id`（没有则为 0）。客户端应使用它或最后一条消息的 id 作为下一次 `after_id`。

响应：

```json
{
  "ok": true,
  "group": "大厅",
  "messages": [ { "id": 12, "...": "..." } ],
  "count": 1,
  "oldest_id": 12,
  "newest_id": 12,
  "latest_id": 12,
  "has_more": true,
  "online_count": 3,
  "online": ["alice", "小明", "Bob"],
  "admins": ["root"],
  "viewer": {
    "nickname": "alice",
    "role": "user",
    "is_admin": false,
    "muted": false,
    "muted_until": null,
    "muted_forever": false,
    "mute_remaining": null,
    "locked": false
  },
  "rev": 22,
  "recall_changes": [],
  "server_time": "2026-10-18T08:30:00Z"
}
```

示例：

```bash
# 最新 20 条
curl "http://127.0.0.1:5050/api/messages?nickname=alice&limit=20"

# 第 100 条之前的 20 条
curl "http://127.0.0.1:5050/api/messages?nickname=alice&before_id=100&limit=20"

# 第 100 条之后的新消息
curl "http://127.0.0.1:5050/api/messages?nickname=alice&after_id=100&limit=200"

# 长轮询，最多等 25 秒
curl "http://127.0.0.1:5050/api/messages?nickname=alice&after_id=100&wait=25"

# 长轮询 + 撤回增量
curl "http://127.0.0.1:5050/api/messages?nickname=alice&after_id=100&since_rev=20&wait=25"
```

> 多行示例使用 bash 的 `\` 续行；Windows cmd 请写成一行，或把 `\` 换成 `^`。

### GET /api/messages/{id}

```bash
curl "http://127.0.0.1:5050/api/messages/12?nickname=alice"
```

```json
{"ok": true, "message": {"id": 12, "...": "..."}}
```

不存在返回 `404`。

---

## 8. 发送文字

### POST /api/messages

Content-Type：`application/json` 或 `application/x-www-form-urlencoded`。

```bash
curl -X POST http://127.0.0.1:5050/api/messages \
  -H "Content-Type: application/json" \
  -d '{"nickname":"alice","text":"大家好！"}'
```

```json
{
  "ok": true,
  "message": {
    "id": 13,
    "nickname": "alice",
    "color": "#5b8cff",
    "kind": "text",
    "text": "大家好！",
    "created_at": "2026-10-18T08:31:00Z",
    "attachment": null
  }
}
```

规则：

- 内容去除首尾空白后不能为空；
- 最长 4000 字符（换行保留，`\r\n` 统一为 `\n`）；
- 返回 `201`；
- 每个昵称 10 秒最多 20 条，超出返回 `429` 与 `Retry-After: 10`。

---

## 9. 上传图片 / 文件

### POST /api/upload

`multipart/form-data`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `file` | 是 | 文件内容 |
| `nickname` | 是* | 昵称，也可用查询参数 / session |
| `text` | 否 | 附带的说明文字（≤4000） |

```bash
# 图片
curl -X POST http://127.0.0.1:5050/api/upload \
  -F "nickname=alice" \
  -F "file=@./photo.png" \
  -F "text=看这张图"

# 任意文件
curl -X POST http://127.0.0.1:5050/api/upload \
  -F "nickname=alice" \
  -F "file=@./report.pdf"
```

响应 `201`，消息的 `kind` 为 `image`（PNG/JPEG/GIF/WebP/BMP/AVIF）或 `file`（其它，包括 SVG/HTML），结构同 Message。

规则：

- 单文件默认最大 50 MB（`CHAT_MAX_UPLOAD_MB`）；
- 文件名仅用于展示 / 下载，磁盘使用随机 UUID 命名；
- 上传内容不可修改或删除；
- 每个昵称 20 秒最多 8 次上传；
- 大小超限返回 `413` / `file_too_large`；缺少文件返回 `400` / `file_required`。

---

## 10. 附件读取 / 下载

### GET /api/files/{id}

需要昵称（浏览器 session 或 `?nickname=`）。

```bash
curl -b cookies.txt -o photo.png "http://127.0.0.1:5050/api/files/4"
curl -b cookies.txt -OJ "http://127.0.0.1:5050/api/files/4?download=1"
curl "http://127.0.0.1:5050/api/files/4?download=1&nickname=alice"
```

行为：

| MIME | 默认 | `?download=1` |
| --- | --- | --- |
| PNG / JPEG / GIF / WebP / BMP / AVIF | `Content-Disposition: inline` | `attachment` |
| SVG / HTML / PDF / 文本 / 其它 | `attachment` | `attachment` |

- 支持 `Range` 请求（图片 / 视频拖动、断点续传）；
- `Cache-Control: public, max-age=31536000, immutable`（附件内容不可变）；
- `X-Content-Type-Options: nosniff`；
- 内联图片额外返回 `Content-Security-Policy: default-src 'none'; sandbox`；
- 不存在返回 `404` JSON。

---

## 11. 只追加约束

```bash
curl -X DELETE "http://127.0.0.1:5050/api/messages/12?nickname=alice"
```

```json
{
  "ok": false,
  "error": {
    "code": "append_only",
    "message": "消息内容不可编辑或删除；如需隐藏，请调用 POST /api/messages/{id}/recall（撤回），POST /api/messages/{id}/unrecall（撤销撤回）"
  }
}
```

`PUT` / `PATCH` / `DELETE` 均返回 `405`。消息不能被改写或物理删除；需要让它从界面上消失时请使用“撤回”（见 [17. 撤回 / 撤销撤回](#17-撤回--撤销撤回)）。

---

## 12. 错误格式

所有 `/api/*` 错误均为 JSON：

```json
{
  "ok": false,
  "error": {
    "code": "invalid_text",
    "message": "消息最多 4000 个字符"
  }
}
```

| HTTP | code | 场景 |
| --- | --- | --- |
| 400 | `invalid_nickname` | 昵称为空 / 超长 |
| 400 | `empty_message` | 文字为空 |
| 400 | `invalid_text` | 文字超长 / 类型错误 |
| 400 | `bad_request` | 数字参数非法 |
| 400 | `file_required` | 上传缺少 `file` |
| 400 | `invalid_role` / `invalid_mute` / `last_admin` | 权限参数非法 |
| 401 | `nickname_required` | 未提供昵称 |
| 403 | `forbidden` | 非管理员 / 撤回他人消息 / 修改 root |
| 403 | `root_protected` | 试图降级 root |
| 403 | `muted` | 被禁言，不能发言 / 上传 |
| 403 | `nickname_locked` | 昵称已锁定，当前 IP 不能使用 |
| 403 | `ip_locked` | 当前 IP 已绑定某个锁定昵称，不能换昵称 |
| 404 | `not_found` | 消息 / 文件 / 接口不存在 |
| 405 | `append_only` | 尝试修改 / 删除消息 |
| 405 | `method_not_allowed` | 方法不允许 |
| 413 | `file_too_large` | 文件超过上限 |
| 429 | `rate_limited` | 发送 / 上传过快 |
| 500 | `storage_error` / `internal_error` | 服务端错误 |

---

## 13. CORS

`/api/*` 返回：

```text
Access-Control-Allow-Origin: *
Access-Control-Allow-Headers: Content-Type, X-Nickname, X-Chat-Client, Range
Access-Control-Allow-Methods: GET, POST, OPTIONS
```

允许浏览器从任意源读取和追加（仍然需要昵称）。注意：使用 `*` 时不支持带 cookie 的跨域请求；跨域脚本请用 `?nickname=` 或请求体昵称。

---

## 14. 代码示例

### Python：发送 + 监听

```python
import requests

BASE = "http://127.0.0.1:5050"
NICK = "alice"

# 发送文字
r = requests.post(
    f"{BASE}/api/messages",
    json={"nickname": NICK, "text": "来自 Python 的问候"},
    timeout=10,
)
r.raise_for_status()
print(r.json()["message"]["id"])

# 上传图片
with open("photo.png", "rb") as fh:
    r = requests.post(
        f"{BASE}/api/upload",
        data={"nickname": NICK, "text": "图片说明"},
        files={"file": ("photo.png", fh, "image/png")},
        timeout=120,
    )
r.raise_for_status()
print(r.json()["message"]["attachment"]["url"])

# 长轮询监听
after = 0
while True:
    data = requests.get(
        f"{BASE}/api/messages",
        params={"nickname": NICK, "after_id": after, "limit": 100, "wait": 25},
        timeout=40,
    ).json()
    for message in data["messages"]:
        print(message["nickname"], message["text"])
        after = message["id"]
```

### JavaScript：浏览器 / Node 18+

```js
const BASE = "http://127.0.0.1:5050";
const NICK = "alice";

async function send(text) {
  const res = await fetch(`${BASE}/api/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nickname: NICK, text }),
  });
  return res.json();
}

let after = 0;
while (true) {
  const res = await fetch(
    `${BASE}/api/messages?nickname=${encodeURIComponent(NICK)}&after_id=${after}&wait=25`
  );
  const data = await res.json();
  for (const m of data.messages) {
    console.log(`[${m.nickname}] ${m.text}`);
    after = m.id;
  }
}
```

### curl：完整发一条图片消息

```bash
curl -X POST http://127.0.0.1:5050/api/upload \
  -F "nickname=alice" \
  -F "text=测试图片" \
  -F "file=@./photo.png"
```

---

## 15. 限制与注意事项

- 昵称不是密码认证：任何昵称都可以读写；“锁定”只是网页端防冒用的手段，API（`X-Chat-Client: api`）不受锁定限制；
- 禁言对 Web 与 API 都生效，禁言期间仍可阅读、撤回自己的消息；
- 消息和附件对所有能访问服务的人可见（撤回后正文不再下发，但数据库里仍在）；
- 长轮询的连接会占用一个线程；小团队足够，大规模请改用事件驱动方案；
- 新消息唤醒使用进程内条件变量，**只能单进程运行**（多线程可以）；
- 附件存储在本机磁盘，备份时请连同 `data/uploads/` 一起复制；
- 如需公网访问，请使用 HTTPS 反向代理并增加真正的访问控制。


---

## 16. 权限管理（管理员）

昵称就是身份。每个昵称有三个独立状态：**角色**（`admin` / `user`）、**禁言**（未禁言 / 定时 / 永久）、**锁定**（是 / 否）。

- 服务初始化时自动写入唯一的管理员 `root`，并把它设为锁定；
- `root` 永远是管理员，不能被降级、不能被别人永久禁言；
- 锁定是“网页端防冒用”：被锁定后，只有该昵称绑定过的 IP 能在网页上使用它，这些 IP 也不能再换成别的昵称；
- 通过 API 调用时带上 `X-Chat-Client: api`（或登录 JSON 里带 `"api": true`）即可绕过锁定；
- 禁言对 Web 与 API **同时生效**，被禁言的昵称不能发言、不能上传，但仍然可以阅读和撤回自己的消息。

### 16.1 权限管理接口

#### GET /api/admin/identities

仅管理员可调用，返回全部昵称的权限状态（按管理员优先、昵称排序，最多 500 条）。

```bash
curl "http://127.0.0.1:5050/api/admin/identities?nickname=root"
```

```json
{
  "ok": true,
  "count": 3,
  "counts": {"total": 3, "admins": 1, "muted": 0, "locked": 1, "online": 2},
  "admins": ["root"],
  "root": "root",
  "mute_presets": [
    {"label": "10 分钟", "seconds": 600},
    {"label": "1 小时", "seconds": 3600},
    {"label": "1 天", "seconds": 86400},
    {"label": "7 天", "seconds": 604800},
    {"label": "永久", "seconds": 0}
  ],
  "rev": 12,
  "identities": [
    {
      "nickname": "root",
      "role": "admin",
      "is_admin": true,
      "muted": false,
      "muted_until": null,
      "muted_forever": false,
      "mute_remaining": null,
      "locked": true,
      "locked_at": "2026-10-25T14:32:02Z",
      "locked_by": "system",
      "locked_ips": ["192.168.1.100"],
      "ips": ["192.168.1.100"],
      "ip_count": 1,
      "message_count": 12,
      "last_seen": "2026-10-25T15:02:11Z",
      "online": true,
      "created_at": "2026-10-25T14:32:02Z"
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `muted` | 当前是否处于禁言期（到期的禁言自动视为未禁言） |
| `muted_until` | 禁言到期时间；永久禁言为 `null` |
| `muted_forever` | 是否永久禁言 |
| `mute_remaining` | 剩余秒数；永久禁言与未禁言为 `null` |
| `locked` | 是否锁定昵称 |
| `locked_ips` | 锁定快照：允许使用该昵称的 IP |
| `ips` | 该昵称历史上登录网页时出现过的 IP（最多 10 个） |
| `online` | 75 秒内是否有活动 |

#### POST /api/admin/identities/{nickname}

修改某个昵称的权限。请求体可以只包含需要修改的字段，`role` / `mute` / `lock` 至少提供一个。

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `role` | `"admin"` / `"user"` | 设为 / 取消管理员 |
| `mute` | `null` | 解除禁言 |
| `mute` | `{"seconds": 600}` | 禁言 10 分钟（`seconds <= 0` 等同永久） |
| `mute` | `{"until": "2026-11-01T00:00:00Z"}` | 禁言到这个时间 |
| `mute` | `{"forever": true}` | 永久禁言 |
| `lock` | `true` / `false` | 锁定 / 解锁昵称 |
| `rebind` | `true` | 仅在 `lock: true` 时有意义：锁定并刷新 IP 快照 |

示例：

```bash
# 禁言 10 分钟
curl -X POST "http://127.0.0.1:5050/api/admin/identities/bob?nickname=root" \
  -H "Content-Type: application/json" \
  -d '{"mute":{"seconds":600}}'

# 永久禁言
curl -X POST "http://127.0.0.1:5050/api/admin/identities/bob?nickname=root" \
  -H "Content-Type: application/json" \
  -d '{"mute":{"forever":true}}'

# 解除禁言
curl -X POST "http://127.0.0.1:5050/api/admin/identities/bob?nickname=root" \
  -H "Content-Type: application/json" \
  -d '{"mute":null}'

# 任免管理员
curl -X POST "http://127.0.0.1:5050/api/admin/identities/bob?nickname=root" \
  -H "Content-Type: application/json" \
  -d '{"role":"admin"}'

# 锁定 / 解锁昵称
curl -X POST "http://127.0.0.1:5050/api/admin/identities/bob?nickname=root" \
  -H "Content-Type: application/json" \
  -d '{"lock":true}'
```

成功时返回更新后的完整身份：

```json
{
  "ok": true,
  "identity": {"nickname": "bob", "role": "user", "muted": true, "muted_until": "2026-10-25T15:20:00Z", "muted_forever": false, "mute_remaining": 600, "locked": false, "locked_ips": [], "ips": ["192.168.1.100"]},
  "rev": 18
}
```

权限限制：

- 非管理员调用返回 `403 forbidden`；
- 非 `root` 管理员不能修改 `root` 的任何权限；
- `root` 不能被降级（`403 root_protected`），也不能被永久禁言；
- 至少要保留一名管理员，取消最后一名管理员返回 `400 last_admin`。

### 16.2 锁定规则

`POST /api/login`（网页登录）会按以下顺序检查：

1. 请求的昵称已锁定，且还没有任何绑定 IP：视为“首次认领”，把当前 IP 绑定上去；
2. 请求的昵称已锁定，但当前 IP 不在绑定列表里：`403 nickname_locked`；
3. 当前 IP 绑定在某个锁定昵称上，却想换成别的昵称：`403 ip_locked`。

补充说明：

- 只有**网页登录**会记录 IP；通过 API 直接带昵称发消息不会影响锁定快照；
- `127.0.0.1` / `::1` 默认豁免（单机部署时所有用户共享本机 IP，否则 root 登录后别人无法再选昵称）。可用 `CHAT_LOCK_IGNORE_IPS=""` 关闭豁免；
- 反向代理后部署时设置 `CHAT_TRUST_PROXY=1`，服务端才会读取 `X-Forwarded-For`；
- 也可以用 `CHAT_ROOT_IP` 在启动前预绑定 root 的 IP，避免“首次登录被抢注”。

### 16.3 初始管理员配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CHAT_ROOT_NICKNAME` | `root` | 初始管理员昵称 |
| `CHAT_ROOT_IP` | 空 | root 的预绑定 IP，逗号分隔 |
| `CHAT_LOCK_IGNORE_IPS` | `127.0.0.1,::1,localhost` | 不做 IP 归属约束的地址 |
| `CHAT_TRUST_PROXY` | 空 | 设为 `1` 时信任 `X-Forwarded-For` |

---

## 17. 撤回 / 撤销撤回

撤回**不是删除**：原文仍然在数据库里，只是对所有客户端隐藏；撤回随时可以撤销。

- 任何人都可以撤回自己的发言；
- 管理员可以撤回任何人的发言；
- 撤回者本人或任意管理员可以撤销撤回；
- 撤回与撤销都会让全局 `rev` 自增，长轮询会立刻返回，并通过 `recall_changes` 下发增量。

### POST /api/messages/{id}/recall

```bash
curl -X POST "http://127.0.0.1:5050/api/messages/12/recall?nickname=alice"
```

```json
{
  "ok": true,
  "recalled": true,
  "rev": 21,
  "message": {
    "id": 12,
    "nickname": "alice",
    "text": "",
    "attachment": null,
    "recalled": true,
    "recalled_by": "alice",
    "recalled_at": "2026-10-25T15:10:00Z",
    "recall_rev": 21,
    "can_recall": false,
    "can_unrecall": true
  }
}
```

### POST /api/messages/{id}/unrecall

```bash
curl -X POST "http://127.0.0.1:5050/api/messages/12/unrecall?nickname=alice"
```

```json
{
  "ok": true,
  "recalled": false,
  "rev": 22,
  "message": {
    "id": 12,
    "nickname": "alice",
    "text": "原来的内容",
    "attachment": null,
    "recalled": false,
    "recalled_by": null,
    "recalled_at": null,
    "recall_rev": 22,
    "can_recall": true,
    "can_unrecall": false
  }
}
```

重复撤回 / 重复撤销是幂等的，不会报错。越权撤回他人消息返回 `403 forbidden`。

### 增量同步 `since_rev`

客户端长轮询时把上一次响应里的 `rev` 带上，就可以收到历史消息的撤回变化：

```bash
curl "http://127.0.0.1:5050/api/messages?nickname=alice&after_id=30&since_rev=20&wait=25"
```

```json
{
  "ok": true,
  "messages": [],
  "rev": 22,
  "recall_changes": [
    {"id": 12, "recalled": true, "recalled_by": "alice", "recalled_at": "2026-10-25T15:10:00Z", "recall_rev": 21}
  ],
  "viewer": {"nickname": "alice", "role": "user", "is_admin": false, "muted": false, "locked": false},
  "admins": ["root"]
}
```

- `since_rev` 省略或为 0 时，`recall_changes` 为空数组；
- `viewer` 每次都会返回当前昵称的最新权限状态，前端据此显示 / 隐藏管理入口、禁言横幅；
- `admins` 是当前管理员名单，前端用来给昵称加“管理员”徽章。

---

## 18. 权限相关错误示例

被禁言时发言：

```json
{
  "ok": false,
  "error": {
    "code": "muted",
    "message": "你已被禁言，还需等待 9 分 58 秒",
    "until": "2026-10-25T15:20:00Z",
    "permanent": false,
    "remaining": 598
  }
}
```

网页登录被锁定拦截：

```json
{
  "ok": false,
  "error": {
    "code": "ip_locked",
    "message": "当前 IP 已绑定昵称「root」，不能切换成其他昵称"
  }
}
```
