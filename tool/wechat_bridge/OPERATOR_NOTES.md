# 微信通信桥接 —— 操作手册（直接上手版）

> 让接手的 AI / 运维人员**无需探索项目代码**，照本手册即可完成"监听 + 发送"。
> v2 · 2026-10-01 · 含实测教训 · 用户与 AI 通过微信聊天（默认"文件传输助手"）双向通信

---

## 0. 系统一览

```
用户手机微信 ←→ 微信聊天(默认"文件传输助手") ←→ PC 微信
                                                   ↑
        serve 服务（127.0.0.1:8900，常驻）—— 操作微信的唯一入口
           ↑（轮询新消息）            ↑（HTTP 发送）
        bridge.py（监听）          wx_send.py（发送）
```

| 部件 | 作用 | 启动方式 |
|---|---|---|
| serve 服务 | 连接微信、提供读取/发送 HTTP 接口 | 后台常驻（本机用异步任务） |
| bridge.py | 轮询新消息 → JSON 输出 + 写 `logs\bridge_log.jsonl` | 定时监控任务（`bridge_start.bat`） |
| wx_send.py | 经 serve 发送消息 | 按需运行 |
| window_watchdog.py | 保持微信窗口够大（防漏消息） | 定时监控任务（`window_watchdog_start.bat`） |

---

## 1. 启动（照抄命令）

**前提**：微信已登录；窗口足够大（见第 2 节）；Python 用 `C:\Users\chenhao\anaconda3\python.exe`。

### 第 1 步：启动 serve 服务（后台常驻）
```bat
cd /d "C:\Users\chenhao\Desktop\DeepChat\client\workspace\workspace\wechat_headless_api"
C:\Users\chenhao\anaconda3\python.exe wx_api.py serve
```
（建议以"后台/异步任务"方式运行；或双击 `一键启动.bat` 自动完成第 1、2 步）

### 第 2 步：启动监听（定时任务）
```bat
C:\Users\chenhao\Desktop\DeepChat\client\workspace\workspace\wechat_bridge\bridge_start.bat
```
→ 用**定时任务**（start_timer）方式运行它；有新消息时会打印 JSON 行（并写日志），值守 AI 会收到通知。

### 第 3 步：启动窗口守护（定时任务）
```bat
C:\Users\chenhao\Desktop\DeepChat\client\workspace\workspace\wechat_bridge\window_watchdog_start.bat
```

> 等效的原生监听方式（备选）：`python wx_api.py monitor --json`。
> 注意：原生 monitor 会与 serve 竞争微信窗口，**仅在 serve 未运行时使用**。

---

## 2. 关键前提：微信窗口必须够大 ⚠️

**实测教训（2026-10-01）**：窗口太小（如 433x415）会导致消息渲染不全 → **漏读消息**（长消息尤其容易漏）！

- 要求：窗口 ≥ 800 宽、≥ 700 高（推荐 1366x850，适配本机屏幕）
- 自动维持：`window_watchdog.py`（每 15 秒检查，变小立即恢复；日志 `logs\window_watchdog.log`）
- 手动修正：`python set_window_size.py`（默认调至 1366x850）

---

## 3. 发送消息

**方式 A（推荐，经 serve）**：
```bat
C:\Users\chenhao\anaconda3\python.exe C:\Users\chenhao\Desktop\DeepChat\client\workspace\workspace\wechat_bridge\wx_send.py --text "你好"
:: 或从文件发送（长文本推荐）：
C:\Users\chenhao\anaconda3\python.exe ...\wx_send.py --file ...\outbox\msg.txt
```

**方式 B（原生 CLI，serve 未运行时的备选）**：
```bat
cd /d ...\wechat_headless_api
C:\Users\chenhao\anaconda3\python.exe wx_api.py send --text "你好" [--chat "对象名"]
```

### 3.1 发送文件 / 图片（2026-10-01 新增 ✅）

serve 新增 `POST /api/sendfile` 接口；wx_send.py 新增 `--attach` 参数：

```bat
:: 单个文件
C:\Users\chenhao\anaconda3\python.exe ...\wx_send.py --attach "C:\path\to\image.png"
:: 多个文件（英文分号分隔）
C:\Users\chenhao\anaconda3\python.exe ...\wx_send.py --attach "C:\a.png;C:\b.png"
:: 指定聊天
... --attach "C:\a.png" --chat "对象名"
```

说明：
- 图片以「文件」形式发送，手机端可点开预览 ✅
- 发送在工作线程串行执行，不与监控竞争窗口
- 已实测：6 个文件（5 张封面 + 1 总览）批量发送成功（2026-10-01 13:10）
- 已实测：Word、PDF 文件发送成功（2026-10-01 13:35）

### 3.2 生成 PDF 交付（2026-10-01 新增 ✅）

手机端查看 docx 可能存在排版错乱，交付 PDF 更稳。管线（工具在 `client\workspace\workspace\cover_design\`）：

```bat
:: 1) docx -> HTML（pandoc 绿色版，公式自动转 MathML）
cd /d ...\cover_design
tools\pandoc-3.1.11.1\pandoc.exe "书.docx" -f docx -t html5 --standalone --embed-resources --mathml -o pandoc_test.html
:: 2) HTML 后处理 + Playwright 渲染 PDF
C:\Python314\python.exe make_pdf.py
```

注意：
- 本机 Python 环境：`C:\Python314\python.exe`（含 python-docx / PIL / playwright）；**anaconda 环境没有 docx 库**
- `beautify_sixue.py`（出版级美化）、`edit_sixue_v2.py`（内容更新）等脚本均可重复运行
- PDF 生成耗时约 1 分钟（90 页）
- **生造字“不六”（U+E000）**：正文含该字符时，先由 `buliu_char_song.png`（透明底小图）内嵌替换，`make_pdf.py` 已内置处理；若新增此类字，需重跑小图渲染

---

## 4. 切换聊天对象（用户可能要求"改成别的"）

编辑 `wechat_headless_api\config.json`：
- `"default_chat"`: 发送的默认目标
- `"chats"`: 监听的会话列表（可多个）

改完**重启 serve + bridge** 生效。发送到指定聊天（不改配置）：
```bat
wx_send.py --text "内容" --chat "好友或群名"
```

---

## 5. 收到消息的处理流程（AI 操作）

1. 读 `logs\bridge_log.jsonl` 尾部几行（或直接看通知内容）
2. 取 `source=wechat` 且 `n` > 游标（`processed_cursor.txt`）的消息
3. `content` 以 `【AI助手` 开头 = 自己发的 → 跳过
4. 其余为**用户消息** → 理解意图 → 执行/回复
5. 回复见第 3 节；完成后把游标更新为已处理的最大 `n`
6. **长任务先发回执**（"收到，处理中"）再发结果——用户对静默等待敏感

---

## 6. 故障处理

| 症状 | 处理 |
|---|---|
| bridge 报 `API 持续失败` | serve 挂了 → 检查/重启 serve（异步任务） |
| 消息漏读 / 读取不全 | 1) 检查窗口大小（第 2 节）2) 检查 serve 是否在运行 |
| serve 异常 | 检查微信窗口未最小化、未移屏外；重启 serve |
| 微信未登录 | 需用户手动登录（无法远程处理） |
| bridge / watchdog 退出 | 重新用定时任务启动对应 bat |
| 电脑重启 | 重启 serve + bridge + watchdog 三个任务 |

---

## 7. 实测记录与经验（2026-10-01）

1. **接收方向**：用户手机消息可稳定捕获（含长消息，窗口够大时）。
2. **窗口大小是命门**：小窗口 → 消息读取只有 0-2 条且丢长消息；拉大窗口后读取恢复（一次读到 4+ 条）。用户通过微信远程提醒"把窗口拉长"，与本地诊断一致。
3. **跨进程竞争**：多个进程同时操作微信窗口会互相干扰；正式发送一律走 `wx_send.py`（经 serve 单实例），直发工具 `wx_send_direct.py` 仅应急。
4. `/api/chats` 会话列表可能为空（微信版本限制，正常）。
5. `serve` 重启后会把"启动时可读到的消息"当作历史基线（不补发通知）；必要时手动读取比对。
6. **回执策略**：处理耗时 >1 分钟的指令先发简短回执（教训：曾让用户等待 4 分钟并焦虑追问）。
7. bridge 通知需等 AI"空闲"时推送（正常机制）。
8. **任务/进程管理（重要）**：`kill_task` 终止定时任务后，内部 python 进程**可能残留**（孤儿进程继续运行）！清理方法：`psutil.process_iter` 按命令行过滤找到 PID → `taskkill /pid <PID> /f`。任何"重启任务"操作后核对 python 进程列表。
9. **三个 Qt 窗口要分清**（窗口操作类脚本必须先筛选）：
   - 主窗口：标题"**微信**"（中文）、类含 QWindowIcon、**可见** ← 只对这一个操作
   - 辅助：标题"Weixin"（英文）、隐藏；以及 ToolSaveBits 工具窗 ← **勿动**
   （脚本 `window_watchdog.py` / `set_window_size.py` 已按此筛选修复）
10. **文件发送能力（2026-10-01 新增）**：serve 增加 `POST /api/sendfile`（`wx_headless.py` 新增 `send_files` 方法），`wx_send.py` 支持 `--attach "a;b;c"`。图片以文件形式发送，手机端可点开预览。实测 6 文件批量发送成功（5 张六学教科书封面 + 总览）。
11. **serve 升级/重启流程（教训）**：改 `wx_headless.py` / `wx_api.py` 后 → `kill_task` → **务必 taskkill 残留进程** → 确认端口 8900 释放 → 重启 → `curl /api/health` 验证。

### 通信记录（2026-10-01）
| 时间 | 方向 | 摘要 |
|---|---|---|
| 12:08 | AI→用户 | 问候（通道建立） |
| 12:08-12:32 | 用户→AI | "好的，我看到了" / "你能看到我发的消息吗" / 保存代码要求 / "不动了？" / "以后可能……"长消息 / "我发现有大问题……" / "窗口太小……" / "你还在吗……" / "收到了！……我担心我发的你有没有看到" |
| 12:10-12:34 | AI→用户 | 依次回复：收到确认 → 保存完成 → 修复窗口说明 → 在线确认 → 能力证明+指南完成说明 |

### 运行状态（最后更新 2026-10-01 13:45）
- 用户出行中；**值守进行中**：serve（含文件发送）+ bridge + watchdog 运行正常
- 本日交付记录：
  - 13:10 六学封面 5 张 + 总览图（微信）
  - 13:30 教材「出版美化版」docx（封面插入 + 全册排版 + 备份）
  - 13:42 内容更新 v2：牛来 / 第八节不六博弈 / 钱院师迁记 / 不六之印 + 两张字图
  - 13:44 90 页 PDF 交付（微信，pandoc+Playwright 管线）
- **恢复方法**：双击 `一键启动.bat`

---

## 8. 关键路径速查

- 桥接目录：`C:\Users\chenhao\Desktop\DeepChat\client\workspace\workspace\wechat_bridge`
- 项目根：`C:\Users\chenhao\Desktop\DeepChat\client\workspace\workspace\wechat_headless_api`
- 消息日志：`wechat_bridge\logs\bridge_log.jsonl`
- 窗口守护日志：`wechat_bridge\logs\window_watchdog.log`
- 处理游标：`wechat_bridge\processed_cursor.txt`
- 发送缓存：`wechat_bridge\outbox\`
- 桌面档（示例）：
  - serve 启动：上文第 1 节
  - 发送：`wx_send.py --file ...\outbox\current.txt`
