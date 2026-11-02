# Chat5050 · 极简单群聊

一个用 **Flask + 原生 Web** 写的聊天软件：只有「大厅」一个群，所有人共读同一份消息流；只需取一个昵称，就能发送文字、图片和文件。除了网页，还提供完整的 **headless JSON API** 和命令行客户端。

- **端口**：默认 `5050`
- **存储**：SQLite + 本地文件，零外部服务
- **前端**：原生 HTML / CSS / JavaScript，无构建步骤、无 CDN
- **消息模型**：追加式（append-only），不可编辑 / 删除；支持撤回（隐藏，可撤销）
- **权限模型**：昵称即身份，分管理员 / 普通用户、禁言 / 未禁言、锁定 / 未锁定
- **实时性**：HTTP 长轮询，新消息与撤回 / 权限变更立即到达

---

## 功能

| 能力 | 说明 |
| --- | --- |
| 单群大厅 | 只有 `大厅` 一个群，所有消息全员可见 |
| 昵称进入 | 无需注册、密码，输入昵称即可读写 |
| 文字消息 | 支持换行、链接自动识别，最长 4000 字符 |
| 图片 | 上传后内联显示，点击查看原图 / 下载 |
| 文件 | 任意类型，显示文件名、大小、下载入口 |
| 多种发送方式 | 选择文件、拖拽到页面、粘贴剪贴板图片 |
| 实时刷新 | 长轮询（`wait=25`），对方发消息立即出现 |
| 断线补拉 | 切后台、网络中断或上传文件后自动补齐漏掉的消息 |
| 历史消息 | 初始加载最新 60 条，可继续“加载更早” |
| 在线状态 | 显示在线人数，点击可展开成员列表 |
| 未读提示 | 向上翻历史时，新消息显示“N 条新消息”气泡 |
| 昵称颜色 | 根据昵称稳定生成头像颜色 |
| 权限管理 | 管理员可任免管理员、禁言（10 分钟 / 1 小时 / 1 天 / 7 天 / 永久）、锁定昵称 |
| 撤回消息 | 本人可撤回自己的发言，管理员可撤回任何人的发言；撤回只是隐藏，可随时撤销 |
| 禁言 | 禁言期间不能发言 / 上传，页面顶部显示倒计时横幅 |
| 锁定昵称 | 只有该昵称绑定过的 IP 能在网页上使用它，这些 IP 也不能改昵称；API 不受限 |
| Headless API | JSON + multipart，可用于脚本 / Bot / 移动端 |
| CLI 客户端 | 登录、发送文字/文件、监听、历史、在线成员 |
| cowsay 机器人 | 可选：牛哞哞把你说的话装进气泡，见下文 |
| 安全防护 | XSS 文本渲染、SVG 强制下载、`nosniff`、CSP、上传大小限制 |

---

## 快速开始

要求 **Python 3.10+**（已在 Python 3.14 + Flask 3.1 上验证）。

```bash
cd chat5050

# 1. 安装依赖
python -m pip install -r requirements.txt

# 2. 启动
python app.py
```

Windows 也可以直接双击 `start.bat`。

启动后打开：

- 本机：<http://127.0.0.1:5050>
- 局域网：终端会打印类似 `http://192.168.1.100:5050` 的地址，同一 Wi-Fi 下的设备可直接访问

首次打开页面会弹出昵称框，输入昵称即可进入大厅。昵称会记住，下次自动进入；右上角可随时切换。

> 说明：这不是账号系统。昵称只用于标记消息，**不做身份校验**。请只在可信网络中使用。

---

## 使用说明

### 发送文字

在底部输入框输入内容：

- `Enter` 发送
- `Shift + Enter` 换行
- 最多 4000 字符，右下角有计数

### 发送图片 / 文件

三种方式：

1. 点击输入框左侧的回形针按钮选择文件；
2. 直接把文件拖进窗口，出现“松开即可发送到大厅”后松开；
3. 截图后在输入框按 `Ctrl + V` 粘贴。

图片会内联显示，点击打开灯箱预览，可下载原图；其他文件显示为文件卡片，点击下载。

单个文件默认最大 **50 MB**，可通过环境变量调整。

### 查看历史

向上滚动到底部出现“加载更早的消息”按钮，点击继续加载。若此时有新消息，不会打断阅读，而是显示“N 条新消息”气泡，点击回到底部。

### 查看在线成员

点击右上角的「N 人在线」即可展开在线成员列表：显示每位成员的昵称、头像颜色和最近活跃时间，自己会带有「你」标记。列表每 15 秒自动刷新，也可以在页面有消息时实时更新。

### 切换昵称

点击右上角昵称胶囊 → 修改昵称 → 进入大厅。历史消息不会丢失，新消息会使用新昵称与颜色。

> 如果昵称被管理员锁定，只有它绑定过的 IP 能继续使用；这些 IP 也不能改成别的昵称。详见下文「权限与管理」。

### 撤回消息

鼠标移到消息上（手机上轻触）会出现「撤回」按钮：

- 每个人都可以撤回自己的发言；
- 管理员可以撤回任何人的发言；
- 撤回后消息变成「该消息已被撤回」占位，其他人立即同步看到；
- 撤回者本人或任意管理员点「撤销撤回」即可恢复显示，随时可以反悔。

撤回**不是删除**：原文仍保存在数据库里，只是不再下发给客户端。

### 权限与管理

初始化时只有 `root` 是管理员，并且 `root` 昵称默认锁定。用 `root` 登录网页后，右上角会出现「管理」按钮：

- 名单里列出所有出现过的昵称，可搜索；
- 「设为管理员 / 取消管理员」调整角色；
- 「禁言…」下拉可选 10 分钟、1 小时、1 天、7 天、永久，已禁言的可「解除禁言」；
- 「锁定昵称 / 解除锁定」控制该昵称能否被他人冒用。

规则：

- 管理员可以控制所有人的权限；
- `root` 永远是管理员，不能被降级，也不能被别人永久禁言；
- 至少要保留一名管理员；
- 锁定只对网页前端有效，headless 调用（`X-Chat-Client: api`）仍然可以自由换昵称；
- 本机地址（`127.0.0.1` / `::1`）默认不做 IP 绑定，方便单机调试；局域网部署时按真实 IP 生效。

---

## Headless API

所有接口都是普通 HTTP + JSON，和网页共用同一条消息流。

Base URL：`http://127.0.0.1:5050`

### 最小示例

```bash
# 读取最近 20 条消息（昵称必填）
curl "http://127.0.0.1:5050/api/messages?nickname=alice&limit=20"

# 发送文字
curl -X POST http://127.0.0.1:5050/api/messages \
  -H "Content-Type: application/json" \
  -d '{"nickname":"alice","text":"大家好！"}'

# 上传图片 / 文件
curl -X POST http://127.0.0.1:5050/api/upload \
  -F "nickname=alice" \
  -F "file=@./photo.png"

# 长轮询：有新消息立即返回（最多等 25 秒）
curl "http://127.0.0.1:5050/api/messages?nickname=alice&after_id=0&wait=25"
```

> 多行示例使用 bash 的 `\` 续行；Windows cmd 请写成一行，或把 `\` 换成 `^`。

### 昵称如何传递

按以下优先级读取，任选一种：

1. URL 参数：`?nickname=alice`
2. JSON 请求体：`{"nickname": "alice", ...}`
3. 表单字段：`nickname=alice`
4. 请求头：`X-Nickname: alice`（中文昵称需按 UTF-8 编码为 latin-1 字节）
5. 网页登录后的 session cookie

推荐脚本使用 **URL 参数** 或 **JSON 字段**，对中文最省事。

### 长轮询

```text
GET /api/messages?nickname=alice&after_id=123&limit=200&wait=25
```

- 服务端最多挂起 25 秒；
- 一旦出现 `id > after_id` 的消息立即返回；
- 超时返回 `messages: []`，客户端用返回的 `latest_id` 继续下一轮；
- 适合脚本实时监听，避免高频轮询。

完整字段、错误码、Python / JavaScript 示例见 **[API.md](API.md)**。

---

## 命令行客户端

`chat_cli.py` 是一个 headless 客户端，依赖 `requests`。

```bash
# 保存昵称并验证服务器
python chat_cli.py login 小明

# 发送文字
python chat_cli.py send "大家好！"

# 上传图片 / 文件（可带说明）
python chat_cli.py send -f ./cat.png --caption "看这只猫"

# 实时监听新消息
python chat_cli.py watch

# 从头开始监听
python chat_cli.py watch --from-start

# 查看最近 30 条
python chat_cli.py history -l 30

# 在线成员 / 当前身份 / 健康状态
python chat_cli.py users
python chat_cli.py me
python chat_cli.py health
```

服务器地址与昵称保存在 `~/.chat5050.json`，也可以临时指定：

```bash
python chat_cli.py -s http://192.168.1.100:5050 -n 小明 send "hi"
```

---

## 牛哞哞 · cowsay 机器人

大厅里还住着一头奶牛。它叫 **牛哞哞**，是一个独立的 Python 进程（`cowsay_bot.py`），
通过 headless API 常驻大厅：你说什么，它就把这句话塞进气泡，配一只 ASCII 小动物回给你。

```
________________________
< 你好呀，我是牛哞哞     >
------------------------
        \   ^__^
         \  (oo)\_______
            (__)\       )\/\
                ||----w |
                ||     ||
```

### 启动

先跑着 `app.py`，另开一个窗口：

```bash
python cowsay_bot.py                 # 默认连 http://127.0.0.1:5050
python cowsay_bot.py --nick 哞哞     # 换个昵称
python cowsay_bot.py --face cat      # 默认形象换成猫
python cowsay_bot.py --width 30      # 气泡宽度（按显示列算，中文占 2 列）
python cowsay_bot.py --mode mention  # 只在被叫到时才回话（安静模式）
python cowsay_bot.py -s http://192.168.1.100:5050
```

Windows 也可以直接双击 **`start_bot.bat`**。

### 在聊天框里使唤它

| 命令 | 作用 |
| --- | --- |
| `/help` | 看帮助 |
| `/牛 你好` | 让奶牛说「你好」 |
| `/猫 你好` | 换猫来说（还有 `/企鹅` `/幽灵` `/兔子` `/龙`） |
| `/想 今晚吃什么` | 变成思考泡泡 |
| `/形象 cat` | 切换默认形象 |
| `/宽度 30` | 调整气泡宽度 |
| `/闭嘴`、`/说话` | 关掉 / 打开自动回话 |
| `/谁` | 查看当前形象、宽度、模式 |

不带 `/` 直接说话时，它会把你的原话画成气泡（这就是 cowsay 的本意）。
另外它有两条自我保护规则：

- 不理 `--ignore` 名单里的昵称（默认 `DeepSeek`），避免两个机器人互相复读；
- 遇到本身就是 ASCII 画的消息就不接话，避免「机器人 × 机器人」无限循环。

### 两个实现细节

1. **按显示宽度排版**：`cowsay_art.py` 用 `unicodedata.east_asian_width` 计算每个字符占几列，
   所以中文、全角标点、emoji 都不会把气泡撑歪。换行优先落在空格处，中文逐字断。
2. **必须用等宽字体渲染**：前端会对看起来像 ASCII 画的消息加 `.msg-text--art`，
   换成 `SimHei` 这类「ASCII 宽度恰好是全角一半」的字体。
   Consolas / Cascadia 的比例是 1.7:1，边框会歪，所以没选它们。

### 相关文件

| 文件 | 作用 |
| --- | --- |
| `cowsay_art.py` | 渲染核心，可单独用：`python cowsay_art.py "你好"` |
| `cowsay_bot.py` | 机器人本体（长轮询 + 命令解析） |
| `start_bot.bat` | Windows 一键启动 |
| `stop_bot.py` | 结束机器人：`python stop_bot.py` |

---

## 配置

通过环境变量调整，全部可选：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CHAT_HOST` | `0.0.0.0` | 监听地址；只想本机访问可设为 `127.0.0.1` |
| `CHAT_PORT` | `5050` | 监听端口 |
| `CHAT_DATA_DIR` | `./data` | 数据库与上传文件目录 |
| `CHAT_MAX_UPLOAD_MB` | `50` | 单文件大小上限（MB） |
| `CHAT_SECRET_KEY` | 自动生成 | Flask session 密钥；默认持久化到 `data/secret_key` |
| `CHAT_ROOT_NICKNAME` | `root` | 初始管理员昵称 |
| `CHAT_ROOT_IP` | 空 | 预绑定给 root 的 IP，逗号分隔（避免首次登录被抢注） |
| `CHAT_LOCK_IGNORE_IPS` | `127.0.0.1,::1,localhost` | 不做 IP 归属约束的地址；设为空字符串则一律约束 |
| `CHAT_TRUST_PROXY` | 空 | 设为 `1` 时信任 `X-Forwarded-For`（放在反向代理后面时用） |

示例：

```bash
# Windows cmd
set CHAT_PORT=5050
set CHAT_MAX_UPLOAD_MB=100
python app.py

# PowerShell
$env:CHAT_MAX_UPLOAD_MB="100"; python app.py
```

---

## 数据与备份

```text
chat5050/
├─ app.py                  # Flask 服务与 API
├─ chat_cli.py             # headless 命令行客户端
├─ templates/index.html    # 页面结构
├─ static/style.css        # 样式
├─ static/app.js           # 前端逻辑
├─ static/favicon.svg
├─ tests/smoke_test.py     # 基础 API 端到端测试
├─ tests/permissions_test.py # 权限 / 撤回端到端测试
├─ tools/ui_preview.py     # UI 流程自测 + 截图（可选）
├─ data/                   # 运行时生成，不纳入版本控制
│  ├─ chat.db              # SQLite：消息、附件索引、在线状态、身份权限
│  ├─ uploads/             # 上传的原始文件
│  └─ secret_key           # session 密钥
├─ requirements.txt
├─ start.bat / start.sh
├─ README.md
└─ API.md
```

备份：停止服务后复制整个 `data/` 目录即可。恢复：把 `data/` 放回项目根目录再启动。

清空聊天记录：停止服务，删除 `data/`，重新启动。

数据库结构（简化）：

```sql
messages(id, nickname, color, kind, body, attachment_id, created_at,
         recalled_at, recalled_by, recall_rev)
attachments(id, stored_name, original_name, mime, size, sha256, created_at)
presence(nickname, color, first_seen, last_seen)
identities(nickname, role, muted_until, muted_forever, muted_at, muted_by,
           locked, locked_at, locked_by, created_at, updated_at)
identity_ips(nickname, ip, first_seen, last_seen)   -- Web 登录过的 IP
lock_ips(nickname, ip, bound_at)                    -- 锁定时的 IP 快照
settings(key, value)                                -- 全局状态版本号 state_rev
```

消息只追加，没有 `UPDATE` / `DELETE` 业务接口；撤回只写 `recalled_at / recalled_by`，原文保留，撤销撤回即清空这两个字段。附件文件使用随机 UUID 文件名，原始文件名仅用于展示与下载。

---

## 设计与实现

### 为什么用长轮询

单文件 Flask 服务不需要 WebSocket 基础设施。客户端携带 `after_id` 发起请求，服务端用 `threading.Condition` 等待新消息，出现即唤醒并返回。对几十人的小群足够，且任何 HTTP 客户端都能直接使用。

> 因为唤醒条件在进程内存中，**请只运行一个进程**（Flask 自带的多线程模式即可）。不要用多 worker 的 WSGI 配置，否则不同 worker 之间不会互相唤醒。

### 在线状态

每次带昵称的请求会更新 `presence.last_seen`（10 秒节流）。75 秒内有请求的昵称视为在线。网页每 25 秒一次长轮询，因此正常浏览时会保持在线。

### 颜色

`sha256(nickname)` → HSL → `#rrggbb`，同一昵称在任何客户端颜色一致。

### 限流

- 文字：每个昵称 10 秒内最多 20 条；
- 上传：每个昵称 20 秒内最多 8 个。

超过返回 `429`，用于防止脚本误刷屏。需要更大规模请自行接入反向代理限流。

### 安全处理

- 所有用户文本（消息、昵称、文件名）都通过 `textContent` 写入 DOM，不拼接 HTML；
- 链接仅识别 `http/https`，加 `rel="noopener noreferrer nofollow"`；
- 上传文件用 UUID 重命名，扩展名白名单式生成，杜绝路径穿越；
- 只有 PNG / JPEG / GIF / WebP / BMP / AVIF 允许内联展示；
- SVG / HTML / 其他类型一律 `Content-Disposition: attachment`，避免存储型 XSS；
- 文件响应带 `X-Content-Type-Options: nosniff`；内联图片带 `CSP: default-src 'none'; sandbox`；
- 页面带 CSP、`X-Frame-Options: DENY`；
- 上传大小同时受 `MAX_CONTENT_LENGTH` 与逐块读取校验保护。

---

## 测试

启动真实服务进程、使用临时数据目录的端到端测试（不会污染 `data/`）：

```bash
# 基础 API：身份、消息、分页、长轮询、上传下载、CORS、限流……（92 项）
python tests/smoke_test.py --port 5059

# 权限与撤回：角色、禁言、锁定、IP 归属、撤回 / 撤销、增量同步（77 项）
python tests/permissions_test.py --port 5061
```

Web 界面已在 Chrome（桌面 1280px + 移动端 390/430px）验证：登录、文字、上传、灯箱、实时推送、未读气泡、昵称切换、XSS 文本原样显示、管理面板、禁言横幅、撤回占位与撤销。

仓库内附两个开发自测脚本（需要本机 Chrome，可用 `CHAT_CHROME` 指定路径）：

```bash
python tools/ui_preview.py       # 走一遍权限 / 撤回流程并截图到 %TEMP%\ui_shots
python tools/ui_layout_check.py  # 纯 DOM 尺寸断言，检查溢出与可见性
```

---

## 部署与安全建议

这是一个**内部小工具**，默认使用 Flask 自带服务器，适合本机 / 局域网 / 小团队：

- 昵称没有密码，任何人都能冒用任意昵称，**不要**当作正式账号系统；“锁定”只能挡住网页端的随意冒用，API 依旧可以自由使用任意昵称；
- 所有拿到地址的人都能读取全部消息与文件，不要发送密码、密钥等敏感信息；
- 暴露到公网时请放在 Nginx / Caddy 后面并启用 HTTPS，同时加访问控制（Basic Auth / VPN / IP 白名单）；
- 上传目录不要配置为可执行；建议以低权限用户运行；
- 需要更正式的生产部署可使用 `waitress-serve --listen=0.0.0.0:5050 app:app`（保持单进程、多线程）。

---

## License

MIT。可自由修改、分发。
