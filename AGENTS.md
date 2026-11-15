# 协作约定（给 AI 助手看）

> 这些是用户在会话中**明确要求过**的规则，按约定执行，不要凭"更稳妥"自行改动。

## 启动 / 重启

- **只启动 `server\run.bat`，不要同时启动 `client\run.bat`。**
  服务端面板会自己把客户端拉起来；两个都开会多出一个 agent 进程，用户明确反对过两次。
- **5000 / 5001 允许直接重启，无需事先征得同意**（2026-11-11 用户明确授权）。
  仍然要求：重启前确认没有正在跑的用户任务；换 exe 后要真跑一次自检。
- 测试优先用**源码版 5002 / 5003**（`PORT=5002 py -3.14 app.py --serve`），
  避免和 5000 抢同一份 `chat_tree.json`；需要验证"真实端口"行为时才重启 5000。

## 交付

- 每次改动只替换三个产物：
  `server\app.exe`、`client\agent.exe`、`server\workspace\client.zip`
- 打包后必须用**新 exe 真跑一次**并抓它自己的页面做自检，不要只看源码。

## 行为

- 用户说"直接做"时**不要再反问**，先做再汇报。
- 报 bug 时优先给出**实测数据**（耗时、字节、日志原文），不要只给推测。
- 修完要说清楚：改了什么、怎么验证的、还有什么没做。

## 打包（2026-11-08 起）

- **全量打包 = pack_all.bat + 安装器**，两步都要做，缺一不可。
  1. `tool\pack_all.bat`         → app.exe / agent.exe / client.zip / server.zip
  2. `tool\sync_uploads.py`      → 同步到三个交付目录
  3. `python tool\installer\make_installer.py` → 重新生成 DeepChat-Setup-vX.Y.exe
     （安装器的 payload 取自 `D:\uploads\DeepChat-可运行版`，所以**必须在 sync 之后**跑）
- 安装器产物：`tool\installer\dist\DeepChat-Setup-vX.Y.exe`
- 版本号改动时，同步改 `make_installer.py` 里的 exe 名与 `installer_gui.py` 的 VERSION。
