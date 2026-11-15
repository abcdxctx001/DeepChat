@echo off
chcp 65001 >nul
rem ============================================================
rem  DeepChat one-click pack (safe to re-run)
rem  Outputs:
rem    1. server\app.exe               (from server\app.py,     port 5000)
rem    2. client\agent.exe             (from client\agent.py,   port 5001)
rem    3. server\workspace\client.zip  (incremental: agent.py/agent.exe/run.bat)
rem  NOTE (2026-11-12, user request): server.zip is no longer assembled.
rem        Installs go through tool\installer\make_installer.py (Setup.exe).
rem  NOTE: if server\app.exe is locked by a running service, the new build is
rem        saved as app_new.exe; run tool\pack\replace_exe.bat after stopping it.
rem  NOTE: old artifacts are backed up to .backups\pack\backup_<timestamp>\
rem
rem  !!! THIS FILE MUST STAY PURE ASCII !!!
rem  cmd.exe parses .bat with the OEM codepage (936 on zh-CN). Multi-byte UTF-8
rem  Chinese in a comment shifts the byte stream and swallows the leading "rem "
rem  of the NEXT line, so the rest of that comment gets executed as a command.
rem  That bug popped a Notepad window with a .ps1 inside on every pack run.
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set "ROOT=%CD%"
rem 2026-11-11: build the timestamp SEPARATELY and only strip spaces from it.
rem   Old code did `set "BAK=%BAK: =0%"` on the FULL path, which also replaced the
rem   space in "DeepChat V2.2" -> "D:\download\DeepChat0V2.2\..." (a phantom 224MB
rem   backup tree outside the project). PowerShell keeps it locale-independent.
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%i"
if not defined TS set "TS=%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%%time:~6,2%"
set "TS=%TS: =0%"
set "BAK=%ROOT%\.backups\pack\backup_%TS%"

rem ---- pick a Python that has PyInstaller (prefer the slim pack venv) ----
set "PYEXE="
rem 2026-11-11: prefer an interpreter that already has PyInstaller; no more mandatory
rem             .backups\venv_pack or Anaconda (that venv step always failed when conda absent).
if exist "%ROOT%\.backups\venv_pack\Scripts\python.exe" set "PYEXE=%ROOT%\.backups\venv_pack\Scripts\python.exe"
if not defined PYEXE (
    py -3.14 -m PyInstaller --version >nul 2>nul && set "PYEXE=py -3.14"
)
if not defined PYEXE (
    py -3 -m PyInstaller --version >nul 2>nul && set "PYEXE=py -3"
)
if not defined PYEXE set "PYEXE=python"
echo [0/5] Python: %PYEXE%

rem ---- make sure PyInstaller exists ----
%PYEXE% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo [0/5] PyInstaller not found, installing...
    %PYEXE% -m pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 30 --disable-pip-version-check || exit /b 1
)

rem ---- back up old artifacts ----
mkdir "%BAK%" >nul 2>nul
for %%F in ("server\app.exe" "server\app_new.exe" "client\agent.exe" "server\workspace\client.zip") do (
    if exist "%ROOT%\%%~F" copy /y "%ROOT%\%%~F" "%BAK%\%%~nxF" >nul
)
echo [1/5] old artifacts backed up to %BAK%

rem ---- build server/app.exe ----
echo [2/5] PyInstaller: server\app.exe (1-4 min)...
mkdir "%ROOT%\.backups\pack\dist" >nul 2>nul
%PYEXE% -m PyInstaller --noconfirm --clean --onefile --noconsole --name app ^
    --icon "%ROOT%\tool\launcher\icon.ico" ^
    --add-data "%ROOT%\server\control.py;." ^
    --add-data "%ROOT%\server\static\icon.ico;static" ^
    --add-data "%ROOT%\server\static\tray.png;static" ^
    --add-data "%ROOT%\server\templates;templates" ^
    --add-data "%ROOT%\server\static;static" ^
    --hidden-import pystray ^
    --collect-submodules pystray ^
    --collect-submodules PIL ^
    --distpath "%ROOT%\.backups\pack\dist" ^
    --workpath "%ROOT%\.backups\pack\build" ^
    --specpath "%ROOT%\.backups\pack" ^
    "%ROOT%\server\app.py"
if errorlevel 1 ( echo [ERROR] app.exe build failed & exit /b 1 )
copy /y "%ROOT%\.backups\pack\dist\app.exe" "%ROOT%\server\app.exe" >nul 2>nul
if errorlevel 1 (
    copy /y "%ROOT%\.backups\pack\dist\app.exe" "%ROOT%\server\app_new.exe" >nul
    echo [!] server\app.exe is locked by a running service; saved as server\app_new.exe
    echo     Stop the old service, then run tool\pack\replace_exe.bat.
) else (
    echo [OK] server\app.exe updated
)

rem ---- build client/agent.exe ----
rem 2026-10-09: bundle the GUI dir (virtual_ui.py / ocr.ps1 / capture_dll / PIL).
rem   Why: virtual_ui.py does `from PIL import Image`. Source runs found PIL via
rem   client/workspace/GUI on sys.path, but the packaged exe had NO GUI dir, so the
rem   import failed and (before the try/except added in agent.py) it killed the whole
rem   agent at startup, reporting a misleading "GUI dir missing" message.
rem   agent.py resolves it as WORK_DIR/GUI first, then _MEIPASS/GUI -> the bundled
rem   copy is the fallback and an external GUI dir still takes precedence.
echo [3/5] PyInstaller: client\agent.exe (GUI/PIL bundled)...
%PYEXE% -m PyInstaller --noconfirm --clean --onefile --noconsole --name agent ^
    --icon "%ROOT%\tool\launcher\icon.ico" ^
    --add-data "%ROOT%\client\control.py;." ^
    --add-data "%ROOT%\client\static\icon.ico;static" ^
    --add-data "%ROOT%\client\static\tray.png;static" ^
    --paths "%ROOT%\client\workspace\GUI" ^
    --add-data "%ROOT%\client\workspace\GUI;GUI" ^
    --hidden-import pystray ^
    --collect-submodules pystray ^
    --collect-submodules PIL ^
    --distpath "%ROOT%\.backups\pack\dist" ^
    --workpath "%ROOT%\.backups\pack\build" ^
    --specpath "%ROOT%\.backups\pack" ^
    "%ROOT%\client\agent.py"
if errorlevel 1 ( echo [ERROR] agent.exe build failed & exit /b 1 )
copy /y "%ROOT%\.backups\pack\dist\agent.exe" "%ROOT%\client\agent.exe" >nul 2>nul
if errorlevel 1 (
    copy /y "%ROOT%\.backups\pack\dist\agent.exe" "%ROOT%\client\agent_new.exe" >nul
    echo [!] client\agent.exe is locked; saved as client\agent_new.exe
) else (
    echo [OK] client\agent.exe updated
)

rem ---- update client.zip (incremental: 7 files replaced) ----
echo [4/5] updating client.zip...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\tool\pack\make_client_zip.ps1" -Root "%ROOT%" -AgentExeNew "%ROOT%\.backups\pack\dist\agent.exe"
if errorlevel 1 ( echo [ERROR] client.zip update failed & exit /b 1 )

rem ---- summary ----
echo.
echo [5/5] ============ PACK DONE ============
for %%F in ("server\app.exe" "server\app_new.exe" "client\agent.exe" "client\agent_new.exe" "server\workspace\client.zip") do (
    if exist "%ROOT%\%%~F" echo   %%~F   (%%~zF bytes)
)
echo.
echo backup dir: %BAK%
endlocal
if not "%1"=="-q" pause
