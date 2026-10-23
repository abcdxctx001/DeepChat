# 虚拟桌面 + 屏幕 OCR + 模拟键盘鼠标（Agent 屏幕操作能力）

为 client agent（`agent.py`）新增的“操作模式”能力：Agent 在**后台隐藏虚拟桌面**上操作目标程序，**完全不抢占用户的鼠标光标与键盘焦点**，用户可正常使用自己的桌面。

> **目录说明（2026-08-13 起）**：本组件（`virtual_ui.py` / `ocr.ps1` / `capture_dll` / 本说明文件）统一存放在
> **`client/workspace/GUI/`** 目录下，`agent.py` 运行时从该目录加载；`agent.exe` 打包版已内置这些组件，
> 同时优先使用 exe 旁 `workspace/GUI` 下的组件（完整包部署时 DLL 路径推导一致）。

## 工作原理

```
用户桌面（不受影响）         隐藏虚拟桌面（AgentDesk）
┌──────────────────┐        ┌──────────────────────────┐
│ 用户正常使用      │        │ 目标程序窗口（隐藏渲染）   │
│ 鼠标/键盘/屏幕    │        │ 输入注入（PostMessage）   │
│                  │        │ DLL注入→WM_PRINT截图      │
└──────────────────┘        └──────────────────────────┘
         ▲                            ▲
         │  localhost:5001            │  CreateDesktopEx + lpDesktop
         └──────────── agent.py ──────┘
```

1. **隐藏桌面**：用 `CreateDesktopEx` 在 WinSta0 下创建 `AgentDesk`，目标程序通过 `STARTUPINFO.lpDesktop` 启动到该桌面 —— 用户桌面完全无感知。
2. **输入注入**：工作线程 `SetThreadDesktop` 到虚拟桌面后，向目标窗口 `PostMessage` 发送 `WM_KEYDOWN/WM_CHAR/WM_LBUTTONDOWN/WM_MOUSEMOVE` 等，支持单击/双击/右键/滚轮/拖拽/热键组合/文本输入。
3. **截图**：隐藏桌面窗口没有显示表面，普通 GDI 截屏拿不到内容。因此把 `capture_dll/capture_helper.dll` 注入目标进程，在进程内 `SendMessageTimeout(WM_PRINT)` 让窗口把内容画到本地 HDC（离屏绘制）；Edit/RichEdit 控件用 `WM_GETTEXT + DrawText` / `EM_FORMATRANGE` 渲染文本。
4. **OCR**：优先 winsdk（`Windows.Media.Ocr`，支持中文），不可用时回退 `ocr.ps1`（PowerShell + WinRT）。

## 使用方式（操作模式下发给 AI，或直接调用）

### REST 接口（agent 端口 5001）

| 接口 | 说明 |
|---|---|
| `POST /vdesk` | `{"action":"create"}` / `{"action":"launch","command":"notepad.exe"}` / `{"action":"list"}` / `{"action":"target","hwnd":"0x..."}` / `{"action":"shot"}` / `{"action":"info"}` / `{"action":"close"}` |
| `POST /ocr` | `{"window":true,"lang":"zh-Hans"}` 截图并 OCR |
| `POST /input` | `{"action":"click","x":150,"y":130}` 等，见下方命令 |
| `GET /vui/status` | 虚拟桌面状态 |
| `GET /vui/shot` | 实时预览截图（返回 PNG，供前端「AI 屏幕监控」轮询展示） |
| `POST /vui/clear_screenshots` | 清除 `workspace/screenshots` 截图缓存目录中的所有文件，返回删除数量与释放字节数 |

### 文本命令（`POST /execute`，供 AI 工具使用）

```
#vdesk create                        # 创建隐藏虚拟桌面
#vdesk launch notepad.exe            # 在虚拟桌面启动程序
#vdesk list                          # 列出窗口（hwnd/类名/标题）
#vdesk target 0x1A2B3C               # 选定目标窗口
#vdesk shot --window                 # 截图（默认目标窗口；--desktop 截全虚拟桌面）
#vdesk info / #vdesk close

#ocr --window [--lang=zh-Hans]       # 截图 + OCR，返回文字与坐标（中心点可用于点击）

#input click 150 130 [--button=right] [--double] [--screen]
#input move 100 200 [--screen]
#input drag 40 110 210 150 [--steps=20] [--button=left]
#input wheel 120 [--x=100 --y=100]
#input key enter                      # enter/tab/esc/f1..f12/a..z/0..9/up/down...
#input hotkey ctrl+c                  # 热键组合：ctrl+shift+s / alt+f4 / win+d
#input type "文本内容"                # 逐字符输入（支持中文）
#input settext "文本"                 # 直接写入目标窗口的 Edit 控件
#input activate                       # 在虚拟桌面内激活目标窗口
```

坐标默认是**目标窗口客户区坐标**（与 `#ocr --window` 截图坐标一致）；加 `--screen` 表示屏幕坐标。

## 文件说明（位于 client/workspace/GUI/）

| 文件 | 说明 |
|---|---|
| `virtual_ui.py` | 核心模块：桌面管理 / DLL 注入 / 截图 / 输入注入 / OCR / 命令解析 |
| `agent.py` | 已集成：`#vdesk`/`#ocr`/`#input` 命令 + `/vdesk`/`/ocr`/`/input` REST 路由（agent.py 本体在 client/ 下，从本目录加载 virtual_ui） |
| `capture_dll/capture_dll.c` | 截图 DLL 源码（注入目标进程，WM_PRINT 离屏渲染） |
| `capture_dll/capture_helper.dll` | 编译产物（x64；如需重新编译运行 `build.bat`） |
| `ocr.ps1` | PowerShell + WinRT OCR 备用方案 |

> 运行期数据目录（`workspace/capture`、`workspace/screenshots`）由 agent 自动创建，不在本目录内。

## 已知限制与兼容性

- **隐藏桌面窗口没有显示表面**，截图依赖窗口响应 `WM_PRINT`：
  - ✅ 自绘 Win32 窗口、Qt、Chromium/Electron、标准控件（Edit 文本经 `WM_GETTEXT+DrawText` 渲染）等可完整截图。
  - ⚠️ tkinter 窗口只能截到控件背景与文字区域框架（Tk 的 `WM_PRINT` 支持有限），按钮等局部内容可能缺失；文本输入仍可用。
  - ❌ DirectX 全屏游戏无法在隐藏桌面渲染（可考虑窗口化 + 引擎自带截图，或使用远程桌面会话模式）。
- **输入注入为消息级**（`PostMessage`）：对基于消息循环的 GUI（Win32/Qt/Tk/Chromium）有效；对使用 `GetAsyncKeyState`/DirectInput 轮询的程序无效（这类程序要求真实输入队列，与“不抢占用户输入”互斥）。
- **OCR 依赖 Windows 语言包**：系统已装 `zh-Hans-CN`，可用 `#ocr --lang=zh-Hans`；无语言包时回退可用语言。
- 目标程序若为 32 位进程，需要编译 32 位版 `capture_helper.dll`（`gcc -m32`）。

## 与 server 的集成

`server/app.py` 已新增三个 AI 工具（`virtual_desktop` / `screen_ocr` / `simulate_input`），
AI 在操作模式下可直接调用；命令通过现有 `command_execute` 通道由前端转发给 client agent（localhost:5001）执行。
前端「AI 屏幕监控」面板支持：面板拖动、全屏/窗口化切换、一键清除截图缓存（`POST /vui/clear_screenshots`）。
