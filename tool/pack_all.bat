@echo off
rem ============================================================
rem  DeepChat 四件套一键打包（可重复执行）
rem  产物：
rem    1. server\app.exe             （由 server\app.py 打包，端口默认 5000）
rem    2. client\agent.exe           （由 client\agent.py 打包，端口默认 5001）
rem    3. server\workspace\client.zip（在旧包基础上仅替换 agent.py/agent.exe/run.bat）
rem    4. server\workspace\server.zip（全新组装；workspace/settings.json 只写空 api_key 占位，不写真实 Key；
rem                                  内含新 client.zip + app.exe + 最新前端模板）
rem  说明：
rem    - 若 server\app.exe 正被运行中的旧服务占用，会自动改存为 app_new.exe 并提示，
rem      待旧服务停止后手动替换（或运行 tool\pack\replace_exe.bat）。
rem    - 打包前的旧产物自动备份到 .backups\pack\backup_<时间戳>\
rem ============================================================
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set "ROOT=%CD%"
set "BAK=%ROOT%\.backups\pack\backup_%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%%time:~6,2%"
set "BAK=%BAK: =0%"

rem ---- 选择 Python（含 PyInstaller）：优先使用精简打包 venv，避免 Anaconda 全家桶导致 exe 膨胀 10 倍 ----
set "PYEXE="
if exist "%ROOT%\.backups\venv_pack\Scripts\python.exe" set "PYEXE=%ROOT%\.backups\venv_pack\Scripts\python.exe"
if not defined PYEXE (
    if exist "C:\ProgramData\Anaconda3\python.exe" set "PYEXE=C:\ProgramData\Anaconda3\python.exe"
)
if not defined PYEXE (
    py -3 -c "import sys;print(sys.executable)" >nul 2>nul && set "PYEXE=py -3"
)
if not defined PYEXE set "PYEXE=python"
echo [0/6] Python: %PYEXE%

rem ---- 若尚无精简 venv，则自动创建（★ 重要：用 conda 全家桶打包会把 MKL/ICU 等
rem        数百 MB 无关 DLL 打进 exe，体积 370MB vs 精简版 21MB，务必使用 venv） ----
if not exist "%ROOT%\.backups\venv_pack\Scripts\python.exe" (
    echo [0/6] 创建精简打包 venv（首次需 2-4 分钟下载依赖）...
    "C:\ProgramData\Anaconda3\python.exe" -m venv "%ROOT%\.backups\venv_pack" || exit /b 1
    "%ROOT%\.backups\venv_pack\Scripts\python.exe" -m pip install --quiet "openai==2.2.0" flask flask-cors chardet requests PyPDF2 python-docx openpyxl python-pptx pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 30 --disable-pip-version-check || exit /b 1
    set "PYEXE=%ROOT%\.backups\venv_pack\Scripts\python.exe"
)

rem ---- 确保 PyInstaller ----
%PYEXE% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo [0/6] 未找到 PyInstaller，正在安装...
    %PYEXE% -m pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 30 --disable-pip-version-check || exit /b 1
)

rem ---- 备份旧产物 ----
mkdir "%BAK%" >nul 2>nul
for %%F in ("server\app.exe" "server\app_new.exe" "client\agent.exe" "server\workspace\client.zip" "server\workspace\server.zip") do (
    if exist "%ROOT%\%%~F" copy /y "%ROOT%\%%~F" "%BAK%\%%~nxF" >nul
)
echo [1/6] 旧产物已备份到 %BAK%

rem ---- 打包 server/app.exe ----
echo [2/6] PyInstaller 打包 server\app.exe（需要 1-4 分钟）...
mkdir "%ROOT%\.backups\pack\dist" >nul 2>nul
%PYEXE% -m PyInstaller --noconfirm --clean --onefile --noconsole --name app ^
    --icon "%ROOT%\tool\launcher\icon.ico" ^
    --add-data "%ROOT%\server\control.py;." ^
    --add-data "%ROOT%\server\static\icon.ico;static" ^
    --add-data "%ROOT%\server\static\tray.png;static" ^
    --add-data "%ROOT%\server\templates;templates" ^
    --add-data "%ROOT%\server\static;static" ^
    --distpath "%ROOT%\.backups\pack\dist" ^
    --workpath "%ROOT%\.backups\pack\build" ^
    --specpath "%ROOT%\.backups\pack" ^
    "%ROOT%\server\app.py"
if errorlevel 1 ( echo [错误] app.exe 打包失败 & exit /b 1 )
copy /y "%ROOT%\.backups\pack\dist\app.exe" "%ROOT%\server\app.exe" >nul 2>nul
if errorlevel 1 (
    copy /y "%ROOT%\.backups\pack\dist\app.exe" "%ROOT%\server\app_new.exe" >nul
    echo [!] server\app.exe 正被占用（5000 端口服务运行中），新包已存为 server\app_new.exe
    echo     待旧服务停止后，运行 tool\pack\replace_exe.bat 完成替换。
) else (
    echo [OK] server\app.exe 已更新
)

rem ---- 打包 client/agent.exe ----
rem ★ 2026-10-09: bundle the GUI dir (virtual_ui.py / ocr.ps1 / capture_dll / PIL).
rem   Why: virtual_ui.py does `from PIL import Image`. Source runs found PIL via
rem   client/workspace/GUI on sys.path, but the packaged exe had NO GUI dir, so the
rem   import failed and (before the try/except added in agent.py) it killed the whole
rem   agent at startup, reporting a misleading "GUI dir missing" message.
rem   agent.py resolves it as WORK_DIR/GUI first, then _MEIPASS/GUI -> the bundled
rem   copy is the fallback and an external GUI dir still takes precedence.
rem   (Keep this file ASCII-only: cmd parses .bat with the OEM codepage.)
echo [3/6] PyInstaller packaging client\agent.exe (with GUI/PIL bundled)...
%PYEXE% -m PyInstaller --noconfirm --clean --onefile --noconsole --name agent ^
    --icon "%ROOT%\tool\launcher\icon.ico" ^
    --add-data "%ROOT%\client\control.py;." ^
    --add-data "%ROOT%\client\static\icon.ico;static" ^
    --add-data "%ROOT%\client\static\tray.png;static" ^
    --paths "%ROOT%\client\workspace\GUI" ^
    --add-data "%ROOT%\client\workspace\GUI;GUI" ^
    --collect-submodules PIL ^
    --distpath "%ROOT%\.backups\pack\dist" ^
    --workpath "%ROOT%\.backups\pack\build" ^
    --specpath "%ROOT%\.backups\pack" ^
    "%ROOT%\client\agent.py"
if errorlevel 1 ( echo [错误] agent.exe 打包失败 & exit /b 1 )
copy /y "%ROOT%\.backups\pack\dist\agent.exe" "%ROOT%\client\agent.exe" >nul 2>nul
if errorlevel 1 (
    copy /y "%ROOT%\.backups\pack\dist\agent.exe" "%ROOT%\client\agent_new.exe" >nul
    echo [!] client\agent.exe 被占用，新包已存为 client\agent_new.exe
) else (
    echo [OK] client\agent.exe 已更新
)

rem ---- 更新 client.zip（增量替换 3 个文件）----
echo [4/6] 更新 client.zip...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\tool\pack\make_client_zip.ps1" -Root "%ROOT%" -AgentExeNew "%ROOT%\.backups\pack\dist\agent.exe"
if errorlevel 1 ( echo [错误] client.zip 更新失败 & exit /b 1 )

rem ---- 全新组装 server.zip ----
echo [5/6] 组装 server.zip（含新 client.zip / app.exe / 梗 api-key；约 1-4 分钟）...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\tool\pack\make_server_zip.ps1" -Root "%ROOT%" -AppExeNew "%ROOT%\.backups\pack\dist\app.exe"
if errorlevel 1 ( echo [错误] server.zip 组装失败 & exit /b 1 )

rem ---- 汇总 ----
echo.
echo [6/6] ============ 打包完成 ============
for %%F in ("server\app.exe" "server\app_new.exe" "client\agent.exe" "client\agent_new.exe" "server\workspace\client.zip" "server\workspace\server.zip") do (
    if exist "%ROOT%\%%~F" echo   %%~F   (%%~zF bytes)
)
echo.
echo 备份目录：%BAK%
endlocal
if not "%1"=="-q" pause
