# DeepChat

**一个能真正操作你电脑的本地 AI 代理。** 基于 Flask + 大模型 API，提供 Web 对话界面，并让模型能执行命令、读写文件、抓取网页、操作屏幕、调度定时任务、派发子代理协作。

> 与普通聊天客户端的区别：DeepChat 的模型不只是"回答"，它能**在你的机器上动手**——跑命令、改文件、开软件、截图识别、起定时任务，过程中自主规划、自行纠错。

---

## 架构总览

**两个独立进程 + 浏览器**，三者职责严格分离：

```
浏览器 ──HTTP :5000──▶ server/app.py ──HTTPS──▶ LLM API（任意 OpenAI 兼容端点）
   │
   └──HTTP :5001（仅回环，浏览器直连）──▶ client/agent.py
        execute / open / open_folder / health / vdesk / ocr / input / vui / task / kill ...
```

| 要点 | 说明 |
|---|---|
| **app 与 agent 从不直连** | 所有机器操作请求都由**浏览器**直接发给本机 agent。因此 server 可部署在远程机器，而 agent 必须与浏览器同机 |
| **agent 只监听回环** | 仅绑定 `127.0.0.1:5001`，对外**零出站请求**，被动等待浏览器调用 |
| **server 只连 LLM** | 唯一的对外网络请求是调用大模型 API |
| **agent 地址从哪来** | 服务端用 `render_template('index.html', agent_url=...)` 渲染成页面里的 `const AGENT_URL`，默认 `http://localhost:5001`，可用环境变量 `AGENT_URL` 覆盖 |

---

## 平台支持

**当前只适配 Windows。** 这是实现层面的限制，不是架构限制：

| 组件 | 能否跑在 Linux / macOS | 原因 |
|---|---|---|
| `server/app.py`（Web UI + 编排） | **可以** | 纯 Python + Flask，只有 2 处 Windows 特化（文件原子替换分支、控制台标题），依赖全是跨平台的 |
| `server/control.py`（控制面板） | 大体可以 | tkinter / pystray 跨平台；`taskkill`、`CREATE_NO_WINDOW` 都有平台分支 |
| `client/agent.py`（本机代理） | **不行** | 重度依赖 Windows：`taskkill /T` 杀进程树、多行命令写临时 `.bat`、`#run_pwsh`、`CREATE_NO_WINDOW`；捆绑的 `mingw64/g++.exe`、`node.exe` 也是 Windows 二进制 |
| `client/workspace/GUI/virtual_ui.py`（屏幕操作） | **不行** | 纯 Win32：`ctypes.wintypes`、`user32`（166 处）、`gdi32`（37 处）、`BitBlt` / `PrintWindow` / `SendInput`，OCR 走系统自带 `winsdk` |
| 启动 / 打包脚本 | **不行** | `run.bat`、`pack_all.bat`、`make_*_zip.ps1`（.NET） |

**实用结论：**

- 想在自己机器上完整使用（能执行命令、操作屏幕）→ **必须 Windows**，因为 agent 必须与浏览器同机。
- 只是想让别人用网页聊天 → **server 可以单独部署到 Linux 服务器**，agent 留在 Windows 本机。
  这正是架构把三者拆开的目的：server 可远程，agent 必须与浏览器同机。

> 移植 Linux 的主要工作在「执行层」而不是架构：`agent.py` 的进程管理/脚本执行换成 POSIX 版本，
> `virtual_ui.py` 需要按 X11/Wayland 重写（截图、键鼠注入、OCR）。

---

## 为什么还要造这个 Harness

市面上已有 DSH、Codex、Claude Code 等更齐全的工具。做这个项目的取舍是：

### 1. 省 Token

- **缓存命中率高**：同一会话连续多轮时，输入大部分命中缓存（实测常在 90% 以上，长会话可达 98%+），
  而缓存价通常是未命中的 1/50（DeepSeek 官方定价 `0.02` vs `1.0` 元/百万 tokens）。
- **主动控制上下文膨胀**：`compress_context` 手动压缩 + 用量达到 50%（可调）自动警告，
  压缩后只保留阶段摘要，丢弃历史。
- 作者实测：同样的轻量任务，费用约为直接使用官方 DSH 的一半。

### 2. 「重启即还原」机房环境的完美适配

- **无状态设计**：没有任何数据库，所有数据都是 JSON 文件
  （`chat_tree.json` 对话树、`plans.json` 任务计划、`workspace/memories/` 记忆库、`settings.json` 配置）。
- 重启后 `list` + `load` 即可把上下文接回来；机房/容器重启不丢工作。
- **轻量**：全仓约 3.2 万行（不含打包进 client.zip 的 node / python / mingw64 运行时），
  装了 Python 就能跑；用 `.exe` 则连依赖都不用装。

### 3. 团队共享的 Web 工作台

- 一人部署，全员打开浏览器就能用（server 可放在内网机器上）。
- 文件与命令都在同一个共享工作区：上传的文件、执行的操作、产生的中间产物，
  所有人看到的是同一份，信息透明。

### 4. 独特的文件交互体验

- 上传 PDF / Office / 代码文件，自动解析内容供模型阅读，并在侧边栏查看器中显示。
- 模型回答里写 `[文件: 完整路径]` 会渲染成**可点击的文件卡片**，点一下直接在本机打开。
- **实时 token 价格计算**：按模型分别计费，区分空闲/高峰两个价位，显示缓存命中率、费用与上下文占用。

---

## 功能

### 两种模式

| 模式 | 用途 |
|---|---|
| **对话模式** | 传统树状多轮对话，支持分支（同一节点多个回答）、图片理解、文件上传。回答生成后会再调一次模型总结 4–10 字标题，失败则回退「新对话」 |
| **操作模式** | Agent 模式：自主规划 → 调用工具 → 观察结果 → 继续，直到完成并输出最终答复 |

### 28 个内置工具（9 个分组，可逐组开关）

| 分组 | 工具 | 默认 |
|---|---|---|
| **文件读写** `file` | `write_file` `replace_in_file` `view_image` `read_file` | ✅ 开 |
| **执行命令** `cmd` | `execute_command` `start_timer` `query_task` `kill_task` `run_script` | ✅ 开 |
| **网页抓取** `web` | `fetch_webpage` `web_search` `web_fetch` | ✅ 开 |
| **记忆管理** `memory` | `save` `delete` `list` `load` `search_memory` | ✅ 开 |
| **任务计划** `plan` | `plan` | ✅ 开 |
| **上下文管理** `compress` | `compress_context` | ✅ 开 |
| **询问用户** `ask_user` | `ask_user` | ✅ 开 |
| **屏幕与桌面操作** `screen` | `virtual_desktop` `screen_ocr` `simulate_input` | ⬜ 关 |
| **子代理管理** `agent` | `spawn_agent` `prompt_agent` `query_agent` `kill_agent` `list_agents` | ⬜ 关 |

### 其他能力

- **子代理协作** —— 主代理可派发多个子代理并行干活，各自独立上下文与里程碑回报（子代理**不能再派子代理**；并发上限默认 3，可在设置里改）
- **定时任务** —— 命令在后台跑，有输出/完成时主动推送通知给模型
- **上下文压缩** —— 长会话自动/手动压缩，保留摘要丢弃历史，避免超出窗口
- **记忆系统** —— 跨会话持久化的本地（`client/workspace/memories`）+ 云端（`server/workspace/memories`）记忆，可按标题/正文搜索；`load` 会把记忆消息**插入当前上下文**，同一轮即可读
- **会话导出/导入** —— 完整记录（含思考、工具调用、用量）导出为 JSON，导入后上下文无缝接续
- **多提供商 / 无限模型** —— 可添加任意多个提供商与模型，各自独立配置地址、密钥、别名、单价
- **四档思考深度** —— Off / Low / High / Max（侧栏滑块，随会话保存）
- **联网工具按次计费** —— `web_search` / `web_fetch` 按**成功请求次数**计费，单价可配（默认 5 元/1000 次，即 0.005 元/次）
- **完整可观测性** —— 实时用量、缓存命中率、费用、耗时、上下文占用分解；余额按提供商分别查询
- **格式化输出** —— Markdown 渲染、代码高亮、数学公式（LaTeX：`$...$` `$$...$$` `\(...\)` `\[...\]` 四种分隔符）

---

## 快速开始

### 方式一：打包版（推荐，无需 Python 环境）

```bat
server\run.bat     :: 控制面板：管理「服务端 + 本机代理」
client\run.bat     :: 控制面板：只管「本机代理」自己
```

会打开一个控制面板，分别启停服务，子进程无黑窗口，可最小化到系统托盘常驻。
也可直接运行 `app.exe --control` / `agent.exe --control`（不带参数默认就是打开面板，`--serve` 才是跑服务）。

> 控制面板是产品自带功能，不是外部工具。
> 端口已被占用时面板显示「端口已占用」并跳过启动，不会重复拉起。

### 方式二：源码运行

```bat
:: 依赖
pip install flask flask-cors openai chardet requests pystray
pip install PyPDF2 python-docx openpyxl python-pptx   :: 可选：文档解析

:: 启动
server\run.bat          :: 控制面板
client\run.bat

:: 或者直接跑服务、看实时日志
python server\app.py
python client\agent.py
```

> `run.bat` 会自动挑选一个已装齐依赖的解释器（优先仓库自带的打包用 venv），都没有才会安装。

### 端口与环境变量

| 项 | 默认 | 环境变量 | 说明 |
|---|---|---|---|
| server 端口 | `5000` | `PORT` | Web 界面 |
| agent 端口 | `5001` | `AGENT_PORT` / `PORT` | 仅 127.0.0.1 |
| 前端连接的 agent 地址 | `http://localhost:5001` | `AGENT_URL` | 由服务端渲染进页面；起在别的端口时要设 |
| server 数据目录 | `server/workspace` | `WORK_DIR_OVERRIDE` | 对话树 / 记忆 / 上传 / 设置；多实例测试用此隔离 |

### 首次配置 API

打开页面后按提示填写，或在「⚙️ 设置 → 💰 模型与价格 → 🧩 模型提供商」里配置。
也可以直接编辑 `server/workspace/settings.json`（exe 为 exe 同级的 `workspace/settings.json`）：

```json
{
  "providers": [
    {
      "name": "DeepSeek",
      "base_url": "https://api.deepseek.com",
      "api_key": "sk-你的密钥",
      "models": [
        {
          "alias": "flash",
          "name": "deepseek-v4-flash",
          "price": { "cached": [0.02, 0.04], "uncached": [1.0, 2.0], "output": [4.0, 8.0] }
        }
      ]
    }
  ],
  "web_tool_price_per_1k": 5.0
}
```

- `price` 单位是 **元 / 百万 tokens**，`[空闲时段, 高峰时段]` 两档；**单价全部由这里决定**，代码里没有硬编码的价格表。
- `web_tool_price_per_1k`：联网工具单价（元 / 1000 次成功请求）。同一页签下有「🌐 联网工具计费」可视化编辑。
- 旧的扁平键（`base_url` / `api_key` / `flash_model` / `pro_model` / `prices`）仍兼容，服务端会把提供商配置**镜像**回这些老键。
- 🔒 API Key 在界面与所有服务端接口中**一律脱敏**（只露末 4 位）；输入框是 password，留空保存 = 保持不变。

### 接入其他模型端点

DeepSeek 专有的思考参数（`thinking` / `reasoning_effort`）**只会发给 DeepSeek 端点**，接第三方时自动省略。
判定依据：`base_url` 含 `deepseek`，或 `api_key` 是 `sk-` 形式。例如接本地 Ollama：

```
Base URL: http://127.0.0.1:11434/v1
API Key:  ollama（任意值）
模型名:    qwen2.5:3b
```

> 小模型的上下文窗口通常不够容纳完整的系统提示词 + 工具定义。若报 `exceeds the available context size`，用 Modelfile 放大：
> ```
> FROM qwen2.5:3b
> PARAMETER num_ctx 32768
> ```

---

## 目录结构

```
DeepChat/
├── README.md               本文件（唯一文档）
├── LICENSE
├── server/
│   ├── app.py              Web 服务 + Agent 编排（约 8500 行）
│   ├── control.py          控制面板（与 client / tool/launcher 三份必须一致）
│   ├── static/             图标 + 前端依赖（markdown / 高亮 / 公式渲染）
│   ├── templates/          前端页面（单文件，约 9700 行）
│   ├── run.bat             双击 → 打开控制面板
│   └── workspace/          运行时数据（会话树 / 记忆 / 上传 / settings.json）
├── client/
│   ├── agent.py            本机操作代理（约 1700 行）
│   ├── control.py          同上（内容必须一致）
│   ├── static/             图标
│   ├── run.bat             双击 → 打开控制面板（只管理自己）
│   └── workspace/
│       ├── GUI/            屏幕操作组件（虚拟桌面 / OCR / 截图 / PIL）
│       ├── fetch_page.js   网页抓取（Node）
│       ├── memories/       本地记忆库
│       └── tool_outputs/   超长工具输出的落盘目录（运行时生成）
└── tool/
    ├── sync_uploads.py     打包产物的分发与校验（开发用）
    ├── pack_all.bat        全量打包（生成 app.exe / agent.exe / client.zip）
    ├── pack/               打包脚本（make_client_zip / make_server_zip / replace_exe）
    ├── launcher/           control.py 的同源副本 + 图标生成 + 控制面板测试
    └── headless_api/       无界面 HTTP 调用接口
```

---

## 打包与交付

```bat
tool\pack_all.bat
```

产出（旧产物自动备份到 `.backups\pack\backup_<时间戳>\`）：

| 产物 | 说明 |
|---|---|
| `server\app.exe` | 服务端可执行文件（默认端口 5000） |
| `client\agent.exe` | 代理可执行文件（默认端口 5001） |
| `server\workspace\client.zip` | 客户端分发包（含 agent / control / GUI / node / python / mingw64） |

> `server\workspace\server.zip` 仍会生成，但**不再作为分发物**（内含 client.zip 会自嵌套，冗余）。

打包需要 PyInstaller；脚本会自动准备精简虚拟环境（`.backups\venv_pack`），
**切勿用 Anaconda 全家桶直接打包** —— 会把 MKL/ICU 等数百 MB 无关 DLL 打进去（exe 从 ~25MB 涨到 ~370MB）。

---

## 安全提示

- **agent 能执行任意命令**。它只监听 `127.0.0.1`，但请勿将其端口暴露到公网。
- **API Key 存在 `server/workspace/settings.json`**，该文件已在 `.gitignore` 中排除，请勿提交。
- 打包进 `server.zip` 的 `settings.json` 只有 `api_key` 为空的占位，**绝不写入真实密钥**。
- 命令执行带基础黑名单（`rm -rf /`、`format`、`shutdown` 等），但**这不是沙箱**，请自行评估风险。
- 操作模式下 AI 执行的命令拥有当前用户权限；超长工具输出会就地落盘到
  `client/workspace/tool_outputs/`，提示里带文件名与绝对路径。

---

## 许可

[MIT](LICENSE)

> **关于 control.py 为什么有多份**：控制面板是产品自带功能，`app.py` / `agent.py` 通过
> `import control` 加载它。分发时用户可能**只拿到 client 目录**（「下载本地代理」给的就是
> client.zip），所以它必须与程序同目录自足。`server/control.py`、`client/control.py`、
> `tool/launcher/control.py`（测试用）**三者必须字节一致**，`sync_uploads.py --check` 会校验。
