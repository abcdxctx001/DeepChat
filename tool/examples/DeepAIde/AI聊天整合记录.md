# DeepCoder × DeepChat 整合记录（AI 聊天分屏）

## 目标
将下载目录中的 DeepCoder（本地竞赛 IDE）与 DeepChat（AI 聊天）整合：
修改 DeepCoder，使其能调出与 AI 聊天的分屏，AI 聊天代码参考 DeepChat 的操作模式。

## 完成内容

### 1. 后端（server.py，已完成）
- `api-key.env` 读取/保存（`read_ai_key` / `save_ai_key`，与 DeepChat 一致）
- `ai_stream_chat()`：纯标准库 urllib 流式调用 DeepSeek API（SSE 逐行解析）
  - 支持 `reasoning_content`（思考流）与 `content`（正文流）
  - 支持 `usage` 统计
- 路由：
  - `GET /api/chat/key` → `{"has_key": true/false}`
  - `PUT /api/chat/key` → 保存 Key
  - `POST /api/chat` → SSE 流式聊天（参数：messages / model / reasoning_effort / thinking_enabled）
- 未配置 Key 时返回 `{"error": "未配置 API Key", "need_key": true}` 引导前端弹窗

### 2. 前端（index.html / app.js / style.css，已补全）
- 工具栏按钮 `💬 AI 聊天`（`#btn-ai-chat`），点击调出右侧聊天分屏
- 聊天面板 `#ai-chat-panel` + 分隔条 `#ai-chat-divider`（可拖拽调整宽度，宽度持久化 localStorage）
- 聊天消息区：用户气泡 / AI 气泡（Markdown 渲染、思考过程可折叠、打字光标动画、工具调用记录展示）
- 输入区：textarea（Enter 发送 / Shift+Enter 换行 / 自动增高）+ 发送/停止按钮
- 🔑 Key 弹窗设置（保存到 api-key.env）
- 清空对话（带确认）
- 状态提示：未配置 Key 时显示
- **操作模式控件（参考 DeepChat，本次补全）**：
  - 模型选择 `#ai-model`：⚡ Flash（deepseek-v4-flash）/ 🧬 Pro（deepseek-v4-pro）
  - 思考深度 `#ai-effort`：Max / High
  - 思考开关 `#ai-thinking-toggle`：显示/隐藏思考过程
  - 设置持久化到 localStorage（`deepcoder.chat.settings`）
  - 每条 AI 回复底部显示 usage 统计（模型/思考深度/输入输出 tokens）

### 3. AI 读写权限（工具调用，参考 DeepChat 操作模式）
- **工具定义 `AI_TOOLS`**（OpenAI function calling 格式）：
  - `list_dir(path)`：列出工作区目录内容
  - `read_file(path)`：读取工作区文本文件（上限 200KB）
  - `write_file(path, content)`：写入/覆盖工作区文件（上限 200KB，自动建目录）
- **工具执行 `execute_ai_tool()`**：
  - 基于 `safe_path` 限制在工作区内（禁止 `../` 逃逸、绝对路径）
  - 禁止写入 `.build` / `.backups` 目录
  - 工具结果回传 API 时截断至 30KB（防撑爆上下文）
- **工具调用循环**：`ai_stream_chat` 支持多轮工具调用（上限 10 轮防死循环），
  流式解析 `delta.tool_calls` → 执行工具 → 结果作为 `tool` 消息回传 → 继续请求
- **SSE 事件**：新增 `tool_call` / `tool_result`，前端在 AI 气泡中展示工具调用记录（🔧 名称/参数/✓✗ 状态/错误详情）
- **系统提示词**：动态注入工作区路径，强制要求使用工具（禁止只输出 ls/cat 等 shell 命令）

### 4. AI 代码补全（ghost text，参考 VSCode Copilot 交互）
- **后端 `/api/ai_complete`**（`ai_complete_suggestion`）：
  - 模型：`deepseek-v4-flash` + thinking disabled + reasoning_effort low（快速响应，实测 0.5~0.9s）
  - 输入：`{path, content, line, ch}`，截取光标前 4000 字符 + 后 800 字符作为上下文
  - **缩进处理**：提示词明确要求缩进与上下文一致（换行后按层级递增、闭合括号对齐）；请求中告知模型光标行缩进量；后端做保守校正（光标在行尾时去掉 AI 重复输出的前导缩进，防双倍缩进）
  - 输出：`{text}` 补全建议（清洗代码块围栏，限长 600 字符，失败返回空）
- **前端触发**：光标静止 **5 秒**（无输入、无移动）且编辑器聚焦、代码文件、无选区、无本地补全弹层时自动请求；编辑器获得焦点（focus）时立即开始计时
- **显示**：以**灰色斜体 ghost text**（`.ai-ghost-text`）插入光标处（markText + atomic），光标保持原位
- **Tab 确认**：接受补全（清除标记、光标移到补全末尾）；继续输入/移动光标/切文件/保存/编译/运行 → 自动取消（ghost 文本不落盘）
- **联动**：与本地补全（Ctrl+Space / `.` `::` `->` 弹层）互斥，本地弹层优先；请求防过期（requestId 校验）
- **调试说明**：调试浮标与日志已按用户要求移除；排查问题时可临时在控制台 `console.log` 观察

## 验证结果（全部通过）
- 服务器启动：主页 200，静态资源（app.js/style.css/index.html）200
- `GET /api/chat/key` → `{"has_key": true}`
- `POST /api/chat`（flash + max + thinking on）：81 个事件（79 thinking + 1 content_delta + 1 done）✅
- `POST /api/chat`（pro + high + thinking off）：29 个 content_delta + done，无 thinking 事件 ✅
- **工具调用（HTTP 端到端）**：
  - 读取测试：AI 调用 list_dir(".") + read_file("workspace/note.md")，正确总结内容 ✅
  - 写入测试：AI 调用 list_dir + write_file + read_file，文件真实写入并读回一致 ✅
- **AI 代码补全（HTTP 端到端）**：
  - C++：光标在 `vector<int> a(n);` 后 → 补全 `for (int i = 0; i < n; i++) cin >> a[i];` ✅
  - Python：光标在读入行后 → 补全多行数据读取 ✅
  - 耗时 0.5~0.9s（flash 不思考）✅
- JS 语法结构检查通过（括号/引号配对）
- index.html 结构检查通过

## 使用说明
1. 双击 `deepcoder.bat` 或运行 `python server.py`（默认端口 8765）
2. 打开页面后点击工具栏 `💬 AI 聊天`
3. 首次使用点 `🔑 Key` 设置 DeepSeek API Key（platform.deepseek.com 获取）
4. 选择模型（Flash/Pro）、思考深度（Max/High）、思考开关
5. 输入消息，Enter 发送；生成中按钮变 ■ 可停止
6. **AI 拥有工作区读写权限**：让它"看一下工作区有什么文件""读取 xxx 并分析""帮我创建/修改 xxx 文件"即可

## 备份
- `.backups/pre_ai_chat/`：AI 聊天功能加入前的版本（app.js/index.html/style.css/server.py）
- `.backups/pre_chat_controls/`：补全操作模式控件前的版本（index.html/app.js/style.css）
- `.backups/pre_tools/`：加入 AI 读写权限（工具调用）前的版本（server.py/app.js/style.css/index.html）
