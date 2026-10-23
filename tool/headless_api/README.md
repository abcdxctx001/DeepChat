# DeepChat Headless API

**无界面调用 DeepChat 的【对话模式】与【操作模式】**（纯 HTTP 接口，供程序/脚本/自动化集成使用）。

本目录是 DeepChat 项目的**独立新增模块**：只新增文件、不修改项目其它任何代码。

---

## 1. 简介

DeepChat 原本只能通过浏览器页面（server 的 Web UI）使用。Headless API 提供了“无头浏览器”能力：

* **对话模式（chat）**：发送消息 → 得到 AI 回复（含思考过程、token 用量），支持树状多轮/分支；
* **操作模式（operation）**：发送任务 → AI 通过工具**真实操作本机**（执行命令、读写文件、抓取网页、屏幕操作、定时任务、子 Agent 等），返回最终答复与完整命令执行记录；
* 全程无需打开浏览器。

### 原理（无头浏览器）

```
┌───────────────┐   HTTP    ┌────────────────────────┐   HTTP   ┌──────────────────┐
│  调用方程序    │ ────────▶ │  tool/headless_api      │ ───────▶ │ server/app.py    │
│ (curl/任何语言)│ ◀──────── │  (本模块, 默认 :5100)    │ ◀─────── │ (对话/操作业务)   │
└───────────────┘           └───────────┬────────────┘          └──────────────────┘
                                        │ HTTP（复刻浏览器行为）
                                        ▼
                            ┌────────────────────────┐
                            │ client/agent.py (:5001)│  本机操作代理（执行命令等）
                            └────────────────────────┘
```

headless_api 用 Python 复刻了前端页面（`server/templates/index.html`）的全部转发逻辑：

| 浏览器行为 | headless_api 的等价实现 |
|---|---|
| 对话模式：POST `/chat` + 轮询 `/api/node/<id>` | 同（`GET /api/chat` 内部完成） |
| 操作模式：POST `/chat`(NDJSON 流) + 解析事件 | 同（后台 reader 线程） |
| 收到 `command_execute` → 转发 agent `/execute` → 回传 `/api/command_result` | 同（后台线程自动完成） |
| `#timer`/`#exec --async` 任务 → 轮询 agent `/task/<id>` → 变化时回传 `/api/timer_notify` | 同（后台 poller 线程自动完成） |
| 多轮：`/api/operation_input` 追加消息 | 同（`POST /api/operate/<sid>/message`） |
| 取消：`/api/cancel_operation` | 同（`POST /api/operate/<sid>/cancel`） |

> 注意：**操作模式需要在与 agent 相同的机器上运行 headless_api**（浏览器架构如此：命令由本机 agent 执行）。
> server 可以部署在远程（通过 `DEEPCHAT_SERVER` 环境变量指向）。

---

## 2. 目录说明

```
tool/headless_api/
├── headless_api.py     # 主程序（HTTP 服务，默认 127.0.0.1:5100）
├── run.bat             # Windows 一键启动（自动检查/安装依赖）
├── test_headless.py    # 自测脚本（健康检查 / 对话模式 / 操作模式）
└── README.md           # 本文档
```

依赖：Python 3.8+、`flask`、`requests`（与 DeepChat server 相同，无需额外安装）。

---

## 3. 快速开始

前置条件（与浏览器使用相同）：

1. 启动 **DeepChat server**（`server/run.bat` 或 `server/app.exe`），默认 `http://127.0.0.1:5000`；
2. 启动 **DeepChat agent**（`client/run.bat` 或 `client/agent.exe`），默认 `http://127.0.0.1:5001`（操作模式必需）；
3. 启动本 headless_api：

```bat
:: 方式一：双击 run.bat；方式二：命令行
cd /d D:\czz\DeepChat\tool\headless_api
python headless_api.py
```

启动后访问 `http://127.0.0.1:5100/health` 检查联通状态：

```bash
curl http://127.0.0.1:5100/health
# {"status":"ok","server":{"connected":true,...},"agent":{"connected":true,...},"sessions":[...]}
```

自测：

```bash
python test_headless.py                # 全量测试（对话 + 操作）
python test_headless.py --chat-only    # 只测对话模式
python test_headless.py --operate-only # 只测操作模式
```

---

## 4. API 文档

所有 POST 请求体均为 JSON（`Content-Type: application/json`），响应为 JSON（流式接口除外）。

### 4.1 通用

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 接口简介 |
| GET | `/health` | 健康检查：server / agent 连接状态、当前操作会话列表 |

### 4.2 对话模式

#### `POST /api/chat` —— 发送消息并获取回复

请求参数：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `message` | string | **必填** | 用户消息 |
| `session_id` | string | 自动生成 | headless 侧对话会话 ID；携带它时自动把新消息挂到上次的节点下（多轮/续接） |
| `parent_id` | string | - | 显式指定父节点 ID（优先于 session_id；用于分支对话） |
| `model` | string | `deepseek-v4-flash` | 可选 `deepseek-v4-pro` |
| `reasoning_effort` | string | `max` | `max` / `high` |
| `thinking` | bool | `true` | 是否启用思考 |
| `files` | array | `[]` | `[{"name":"文件名","content":"文本内容"}, ...]`（先自行提取文件文本） |
| `wait` | bool | `true` | true=等待生成完成再返回；false=立即返回 node_id |
| `timeout` | number | 300 | 等待超时（秒） |
| `stream` | bool | `false` | true 时返回 NDJSON 流：`node` → `reasoning`/`delta`（增量）→ `done` → `result` |

响应（非流式）：

```json
{
  "ok": true,
  "node_id": "1789...",
  "session_id": "hl-chat-xxx",
  "parent_id": null,
  "status": "answered",
  "reply": "AI 的完整回复",
  "reasoning": "思考过程文本",
  "reasoning_duration": 3.2,
  "title": "自动生成的对话标题",
  "usage": {"prompt_tokens": 315, "completion_tokens": 83, "total_tokens": 398, "cached_tokens": 0}
}
```

示例（Python）：

```python
import requests
r = requests.post('http://127.0.0.1:5100/api/chat',
                  json={'message': '你好', 'timeout': 180})
d = r.json()
print(d['reply'])

# 多轮：携带上次返回的 session_id
r2 = requests.post('http://127.0.0.1:5100/api/chat',
                   json={'message': '继续说说', 'session_id': d['session_id']})
print(r2.json()['reply'])
```

示例（curl）：

```bash
curl -X POST http://127.0.0.1:5100/api/chat \
     -H "Content-Type: application/json" \
     -d "{\"message\": \"你好\", \"timeout\": 180}"
```

#### `GET /api/chat/node/<node_id>` —— 查询节点详情
#### `GET /api/chat/tree` —— 查询完整对话树

### 4.3 操作模式

操作模式是**会话制**：创建会话（发送第一条消息）→ 会话保持在线（AI 可执行多轮任务）→ 追加消息 → 取消/关闭。

#### `POST /api/operate` —— 新建会话并发送第一条消息

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `message` | string | **必填** | 任务描述 |
| `model` / `reasoning_effort` / `thinking` | - | 同对话模式 | - |
| `settings` | object | 见下 | 工具开关 / 超时 / 截断等设置 |
| `wait` | bool | `true` | 等待最终答复；false 立即返回会话 ID |
| `timeout` | number | 600 | 等待最终答复超时（秒） |
| `require_agent` | bool | `true` | agent 不在线时直接报错；设 false 可跳过检查（命令将执行失败） |

`settings` 默认值（与网页设置一致）：

```json
{
  "tools": {"file":true,"web":true,"timer":true,"screen":true,
             "memory":true,"plan":true,"agent":true,"compress":true},
  "idle_timeout": 30, "max_timeout": 600,
  "process_refresh": 5, "process_truncate": 4000, "list_limit": 50
}
```

响应（wait=true，成功时）：

```json
{
  "ok": true,
  "status": "answered",
  "session_id": "hl-op-xxx",          // headless 会话 ID（后续接口用）
  "server_session_id": "uuid",         // DeepChat server 侧会话 ID
  "turn_done": 1,
  "answer": "最终答复",
  "thinking": "最后一轮的思考过程",
  "commands": [
    {"task_id":"abc123","command":"#exec echo hi","command_title":"执行命令",
     "output":"hi","returncode":0,"server_side":false,"agent_id":null}
  ],
  "usage": {"prompt_tokens":123,"completion_tokens":45,"total_tokens":168,"cached_tokens":0},
  "closed": false, "cancelled": false, "last_error": null
}
```

`status` 取值：`answered`（已完成）/ `timeout`（等待超时，会话仍在线，可用 status 继续查询）/ `closed`（流断开）/ `cancelled`（已取消）。

#### `POST /api/operate/<session_id>/message` —— 追加消息（多轮）

请求体：`{"message": "...", "wait": true, "timeout": 600, "settings": {...可选}}`。
响应同上（`turn_done` 递增）。

```python
import requests
BASE = 'http://127.0.0.1:5100'

d = requests.post(BASE + '/api/operate', json={
    'message': '在 D:/test 目录下创建一个 hello.txt，内容为 你好'}).json()
print(d['answer'])

d2 = requests.post(BASE + '/api/operate/%s/message' % d['session_id'], json={
    'message': '读取刚才那个文件的内容并告诉我'}).json()
print(d2['answer'])
```

#### `GET /api/operate/<session_id>/status` —— 会话状态

返回：`busy`（AI 是否处理中）、`turn_done`、`answers`（全部答复）、`last_answer`、
`commands`、`tasks`（仍在跟踪的定时/异步任务）、`closed`、`cancelled` 等。

#### `GET /api/operate/<session_id>/events?after=<seq>&limit=<n>` —— 事件流记录（增量拉取）

返回自 `after` 之后的事件（NDJSON 原始事件 + `seq`/`ts` 包装），用于审计/追踪全过程：

* `thinking` / `content_delta`：思考/正文增量
* `command_execute` / `command_result`：命令请求与结果
* `final_answer`：某轮最终答复
* `usage`：token 统计
* `user_message`：系统注入通知（如定时任务完成唤醒）
* `task_update`：headless 轮询到的定时/异步任务状态变化
* `plan_update`：任务计划（#plan）
* `agent_*`：子 Agent 事件（带 `agent_id`）

#### `POST /api/operate/<session_id>/cancel` —— 取消（终止）会话

终止 AI 执行、关闭事件流；会话对象保留（可用 status 查看 `closed:true`）。

#### `POST /api/operate/<session_id>/close` —— 关闭并释放会话

同 cancel，并从 headless 注册表移除（彻底清理）。

#### `GET /api/sessions` —— 列出全部操作会话

### 4.4 定时/异步任务说明

操作模式中 AI 可以调用 `start_timer`（定时监控，完成时自动通知 AI 继续处理）或
`#exec --async`（后台任务，需 AI 主动查询）。headless_api 会自动：

* 识别 `#timer` / `#exec --async` 命令返回的任务 ID 并后台轮询；
* 定时任务状态变化时自动回传 server（唤醒 AI 输出新答复，`turn_done` 递增）；
* 相关明细可在 `commands` / `tasks` / `events`（`task_update`）中查看。

---

## 5. 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPCHAT_SERVER` | `http://127.0.0.1:5000` | DeepChat server 地址 |
| `DEEPCHAT_AGENT` | `http://127.0.0.1:5001` | DeepChat agent 地址 |
| `HEADLESS_HOST` | `127.0.0.1` | 本 API 监听地址（默认仅本机；如需局域网访问改为 `0.0.0.0`） |
| `HEADLESS_PORT` | `5100` | 本 API 监听端口 |

示例：

```bat
set HEADLESS_PORT=5200 && python headless_api.py
set DEEPCHAT_SERVER=http://192.168.1.10:5000 && python headless_api.py
```

---

## 6. 常见问题

**Q1：`/health` 显示 server/agent 未连接？**
先启动 DeepChat server / agent（操作模式必须启动 agent）；确认端口与地址一致。

**Q2：操作模式报 503 “本地 agent 未连接”？**
agent 未启动。浏览器同样要求 agent 在线；若只想不限检查，传 `"require_agent": false`。

**Q3：端口被占用？**
用 `HEADLESS_PORT` 换一个端口（如 5200）。

**Q4：启动时 OSError WinError 10038？**
某些环境（如 Anaconda）残留 `WERKZEUG_SERVER_FD` 环境变量所致；本程序已自动清除该变量，无需处理。

**Q5：等待超时（status=timeout）怎么办？**
AI 可能还在执行长任务。可继续 `GET /status` 查询（`busy` 表示处理中），或调大 `timeout`。

**Q6：操作模式会真的执行命令吗？**
会——与网页操作模式完全一致，命令由本机 agent 执行，拥有当前用户权限。请在受控环境使用，并可借助 `settings.tools` 开关限制能力（如关闭 `file`/`screen` 组）。

**Q7：如何保证不干扰用户正在使用的网页会话？**
headless 的操作会话与网页会话完全隔离（独立 session）；对话模式的树状节点会保存在同一对话树中（与网页共用），如需隔离可部署第二实例（`WORK_DIR_OVERRIDE` 环境变量隔离 server 数据目录）。

---

## 7. 设计说明与边界

* **不修改其它代码**：本模块不 import / 不修改 server、client 的任何文件，仅通过 HTTP 交互；
* **与网页行为一致**：命令转发、结果回传、定时轮询、设置项等均按 `templates/index.html` 前端逻辑等价实现（含 `server_side` 命令跳过、`#mem_*` 静默执行、子 Agent 命令带 `agent_id` 回传等）；
* **会话生命周期**：操作会话的 NDJSON 长连接由 headless 保持；进程退出/接口 close 时自动断开，server 随之清理会话；
* **线程模型**：每个操作会话 1 个 reader 线程 + 1 个 poller 线程；命令执行按事件并发，互相独立；
* **安全**：默认仅监听 `127.0.0.1`；操作模式可执行系统命令，请勿将 5100 端口暴露到不可信网络（如需暴露，请自行加鉴权反向代理）。

## 8. 实测记录（2026-09 本机）

| 用例 | 结果 |
|---|---|
| 对话模式：单轮问答（回复“测试通过”） | ✅ 2.8s |
| 对话模式：多轮记忆（“记住 42”→“42”） | ✅ 上下文续接正常 |
| 操作模式：执行 `echo` 并汇报 | ✅ 命令经本机 agent 执行并回传 |
| 操作模式：多轮追加消息 | ✅ turn 递增、状态正常 |
| 操作模式：定时任务（`ping` × 2）完成唤醒 | ✅ 自动通知 AI 输出新答复 |
| 操作模式：status / events / cancel / close | ✅ 全部正常 |
