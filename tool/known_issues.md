# 待查问题（Next session TODO）

> 更新时间：2026-11-07
> 下面 BUG-1 / BUG-2 的根因已定位并修复（提交 `4eb2d83`），**等你实测确认**
> 环境：操作模式 · 源码版 5002（`D:\downloads\DeepChat`）· 5000 仍跑旧 exe

## ✅ 已修（`4eb2d83`，仅 5002 生效）

### BUG-1 操作模式自动保存用了**对话模式的标题**

- **根因**：`autosaveTitle()` 在没有 plan 时会回退到**聊天树当前节点标题**
  （`treeData.nodes[selectedNodeId].title`），而操作模式根本没有自己的标题来源。
  于是备份被命名成对话模式里的主题，例如 `【自动保存】资源监视器绿蓝线区别.json`。
- **修法**：操作模式只用「操作会话自己的标题」
  （`operationTitleOverride` → `sessionInfoTitleOverride` → 当前计划标题 → `操作任务`），
  **绝不读聊天树**。

### BUG-2 自动保存**没有工具过程**

- **根因（两处）**：
  1. 前端 `buildBackupPayload()` 在操作模式只挑 `role` + `content` 字符串，
     **tool 消息 / tool_calls / reasoning 全部被过滤掉** → 发给服务端的 payload 天生残缺。
  2. 服务端 `_OP_INTERNAL[session_id]` 的刷新点原来只插了 1 处
     （另一个 `resolve_server_command` 调用写法不同没匹配上），可能从来没被赋值。
- **修法**：
  1. 前端请求带上 `session_id`（顶层 + payload 内各一份）
  2. 服务端 `_OP_INTERNAL` 刷新点补到 **2 处**，覆盖所有工具调用入口
- **验证方法**：跑一轮带工具的操作任务 → 看日志有没有
  `[autosave] 使用内部消息 N 条（含工具过程）`；
  再打开 `server/workspace/memories/【自动保存】*.json` 确认存在 `"role": "tool"`。

## ⏳ 如果验证仍失败，从这里继续

1. 前端 `operationTitleOverride` 是否真的被赋值（可能一直是 `null`）
2. 事件流里的 `sessionId` 与 `/api/autosave` 带的 `operationSessionId` 是否同一个
3. 兜底方案：**服务端自己在操作循环末尾周期性落盘**，
   彻底不依赖前端的数据与标题

## 备注

- 5000（czz）跑的是旧 exe，以上修复都**没有**进 5000；需要打包 + 重启才生效
- 用户明确要求：**不要随便重启 5000**，要动先问
- 相关提交：`757a271`（记录）→ `4eb2d83`（修复）
