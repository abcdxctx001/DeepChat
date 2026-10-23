# DeepChat 一键打包工具（tool\pack_all.bat）

> 以后修改完代码（`server/app.py`、`server/templates/index.html`、`client/agent.py` 等）需要发布时，
> 直接运行 `tool\pack_all.bat` 即可重新生成**三件套**，可重复执行。

## 产物与位置

| 产物 | 说明 |
|---|---|
| `server\app.exe` | 服务端（PyInstaller onefile，由 `server/app.py` 打包；**默认端口 5000**） |
| `client\agent.exe` | 客户端代理（由 `client/agent.py` 打包；**默认端口 5001**） |
| `server\workspace\client.zip` | 客户端完整包：在旧包基础上**增量替换** `client/agent.py`、`client/agent.exe`、`client/run.bat`、`client/control.py`、`client/static/*`，内置的 python/node/mingw64/GUI 运行时保持不变 |
| `server\workspace\server.zip` | 服务端分发包：全新组装（README / `workspace\settings.json` 占位 / app.py / app.exe / run.bat / static / templates / workspace\client.zip）。**已不再作为分发物**（内含 client.zip 会自嵌套，冗余），打包脚本仍会生成它 |

> 分发用的三个交付目录由 `tool\sync_uploads.py` 同步（源码版 / 出厂设置版 / 可运行版），
> 它有自己的白名单，不会把测试产物、日志、真实 Key 带出去。

## 关键行为

1. **API Key 占位**：`server.zip` 内的 `server/workspace/settings.json` 只写入 `api_key`
   为空的初始占位（`base_url` 为默认值），**绝不写入真实密钥**；
   部署方启动后在页面「设置 → 💰 模型与价格 → 🧩 模型提供商」里填自己的 Base URL 与 Key。
   （旧的「🔌 API 接入」表单已随多提供商改造删除。）
2. **端口**：`server\run.bat` 打开 `http://localhost:5000`；`app.py` 默认 5000、
   `agent.py` 默认 5001（`PORT` / `AGENT_PORT` 可覆盖；前端连的 agent 地址可用 `AGENT_URL` 覆盖）。
3. **占用处理**：若 `server\app.exe` 正被运行中的旧服务占用而无法覆盖，新包自动保存为
   `server\app_new.exe`（agent 同理 `client\agent_new.exe`），日志会提示；
   停止旧服务后运行 `tool\pack\replace_exe.bat` 完成替换。
4. **备份**：每次打包前自动把旧产物备份到 `.backups\pack\backup_<时间戳>\`。
5. **control.py 一致性**：`server/control.py`、`client/control.py`、`tool/launcher/control.py`
   必须字节一致；`python tool\sync_uploads.py --check` 会校验，不一致直接失败。

## 依赖与体积说明（★ 重要）

- **打包环境**：脚本默认使用 `.backups\venv_pack`（自动创建的精简 venv，仅装运行所需依赖）。
  切勿用 Anaconda/conda 全家桶直接打包——会把 MKL/ICU/Qt 等**数百 MB 无关 DLL**打进去，
  exe 会从 ~25MB 膨胀到 ~370MB（实测：Anaconda 版解压 923MB vs 精简版 ~55MB）。
- Python 3.8 环境装 openai 需锁定版本：`openai==2.2.0`（更新版要求 Python≥3.9）。
- 打包机要求 Python 3.8+（含 PyInstaller；首次运行会自动 pip 安装 PyInstaller）
- PowerShell（Windows 自带），zip 更新使用 .NET System.IO.Compression

最近一次实测（2026-10-21，精简 venv）：

| 产物 | 体积 |
|---|---|
| `server\app.exe` | **24.4 MB** |
| `client\agent.exe` | **16.0 MB** |
| `client.zip` | **184.1 MB** |
| `server.zip` | 205.6 MB（不再分发） |

## 手动执行分步命令（等价）

```bat
:: 打包 app.exe（输出到 .backups\pack\dist\app.exe 后复制到 server\）
python -m PyInstaller --noconfirm --clean --onefile --name app ^
  --distpath ".backups\pack\dist" --workpath ".backups\pack\build" --specpath ".backups\pack" server\app.py

:: 打包 agent.exe（virtual_ui 运行时从 client\workspace\GUI 动态加载，不进 exe）
python -m PyInstaller --noconfirm --clean --onefile --name agent --exclude-module virtual_ui ^
  --distpath ".backups\pack\dist" --workpath ".backups\pack\build" --specpath ".backups\pack" client\agent.py

:: 更新 client.zip / 组装 server.zip
powershell -ExecutionPolicy Bypass -File tool\pack\make_client_zip.ps1 -Root "%CD%"
powershell -ExecutionPolicy Bypass -File tool\pack\make_server_zip.ps1  -Root "%CD%"
```

## 常见问题

- **pyinstaller 报 no module named 'openai' 等**：请在带完整依赖的 Python 环境执行。
  脚本优先用 `.backups\venv_pack`，没有时才退回 `C:\ProgramData\Anaconda3\python.exe`
  —— 但用 Anaconda 打包出的 exe 会大十倍，不推荐。
- **zip 更新慢**：`make_server_zip.ps1` 需要把约 184MB 的 client.zip 压进 server.zip，
  耗时约 1~5 分钟属正常；`make_client_zip.ps1` 为增量更新（秒级）。
- **验证**：打包完成后可用 Python 检查
  `zipfile.ZipFile(...).read('server/workspace/settings.json')` 里的 `api_key` 应为空串，
  不含真实 Key。
