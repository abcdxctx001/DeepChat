# 微信桥接工具包（wechat_bridge）

> 让 AI / 程序通过微信「文件传输助手」与用户双向通信的轻量工具集
> v1 · 2026-10-01 · 已完成双向实测
> 基于 [`wechat_headless_api`](../wechat_headless_api)（wxauto4 + UIAutomation 无界面操作微信）

## 功能

- **监控**：实时轮询指定聊天的新消息（默认"文件传输助手"），每条消息输出一行 JSON + 写入日志
- **发送**：向指定聊天发送文本消息（超长自动分片）
- **保持唤醒**：监控进程阻止系统休眠（进程退出即自动恢复，不修改系统设置）

## 文件清单

| 文件 | 用途 |
|---|---|
| `bridge.py` | 监控桥接主程序：轮询新消息 → 输出 JSON 行 + 写 `logs\bridge_log.jsonl` |
| `bridge_start.bat` | bridge 启动脚本（供系统监控任务/手动使用） |
| `wx_send.py` | 发送消息（经 HTTP 服务，**推荐**）：`--file 文件` / `--text 文本` / `--attach 文件路径`（发文件/图片，多个用 `;` 分隔）/ stdin |
| `wx_send_direct.py` | 发送消息（独立进程直连微信，备用；与 serve 并发时可能不稳定） |
| `diag_read.py` | 诊断工具：读取当前聊天全部消息（与 serve 并发时结果可能不全） |
| `一键启动.bat` | 一键启动 serve + bridge 两个窗口（日常最省事） |
| `OPERATOR_NOTES.md` | 操作手册（含故障处理、实测记录） |
| `README.md` | 本文件 |

运行时生成：`logs\`（日志）、`outbox\`（发送内容缓存）、`processed_cursor.txt`（已处理消息游标）

## 快速开始（3 步）

1. 确保 Windows 微信已**登录**，且窗口未被最小化（微信 4.x 要求窗口可见才能读取）
2. **启动服务**（后台常驻，端口 8900）：
   ```bat
   cd /d "C:\Users\chenhao\Desktop\DeepChat\client\workspace\workspace\wechat_headless_api"
   C:\Users\chenhao\anaconda3\python.exe wx_api.py serve
   ```
3. **启动监控**：双击 `一键启动.bat`（或单独双击 `bridge_start.bat`）

## 常用操作

发送消息：
```bat
C:\Users\chenhao\anaconda3\python.exe wx_send.py --text "你好"
C:\Users\chenhao\anaconda3\python.exe wx_send.py --file outbox\msg.txt
```

发送文件 / 图片（2026-10-01 新增）：
```bat
C:\Users\chenhao\anaconda3\python.exe wx_send.py --attach "C:\path\image.png"
:: 多个文件用英文分号分隔
C:\Users\chenhao\anaconda3\python.exe wx_send.py --attach "C:\a.png;C:\b.png"
```

接收消息（在程序里消费）：
- 实时：读取 bridge 输出（每行一条 JSON）
- 历史：读 `logs\bridge_log.jsonl`
- JSON 字段：`n`=毫秒时间戳ID、`content`=内容、`sender`（self/friend）、`chat`、`timestr`

健康检查：
```bat
curl http://127.0.0.1:8900/api/health
```

## 消息约定

- AI 发出的消息以 `【AI助手】` 开头；用户消息一般不带该前缀
- 消息去重键 = (内容, 时间戳, 方向)；连续发送**完全相同**内容可能被合并（已知限制，必要时在消息中加时间戳）

## 已知现象（实测）

- **微信窗口必须足够大（≥800x700，推荐 1366x850）**：窗口太小会导致消息渲染不全、漏读长消息（已实测）。`window_watchdog.py` 会自动维持窗口尺寸。
- 用户从手机发的消息与 PC 端发出的，数据结构上同为 `sender=self`（正常，不影响使用）
- 独立进程 `wx_send_direct.py` 与 serve 并发时消息可能不一致（窗口竞争）→ 正式发送请一律走 `wx_send.py`
- `/api/chats` 的会话列表可能返回空（微信版本限制，正常现象）
- 微信窗口最小化/移出屏幕时 UIA 读不到内容 → 脚本会自动尝试恢复窗口

## 依赖与环境

- Python：`C:\Users\chenhao\anaconda3\python.exe`（3.12 已装 wxauto4 / flask）
- 微信 PC 版：4.1.8.107（wxauto4 免费版支持上限，**请勿升级**）
- 系统：Windows 10 / 11

## 版本记录

- **v1 / 2026-10-01**：首版。建立"监控 + 发送"完整链路，完成用户↔AI 双向通信实测（链路稳定，消息实时收发）。
