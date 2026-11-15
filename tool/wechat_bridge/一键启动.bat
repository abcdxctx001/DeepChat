@echo off
rem ---- 解析 python：优先 PATH 里的 python，其次 py -3 ----
set "PYEXE="
for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYEXE set "PYEXE=%%P"
if not defined PYEXE (where py >nul 2>nul && set "PYEXE=py -3")
if not defined PYEXE (
    echo [错误] 没找到 python，请先安装 Python 3 并加入 PATH
    pause
    exit /b 1
)
title WeChat Bridge - One Click Start
setlocal

echo ============================================
echo  WeChat Bridge : start serve + bridge + watchdog
echo ============================================

netstat -ano | findstr ":8900" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo [skip] Port 8900 already listening - serve seems running.
) else (
    echo [1/3] Starting serve ...
    start "wx-serve" cmd /k "cd /d %~dp0..\wechat_headless_api && set PYTHONIOENCODING=utf-8 && "%PYEXE%" wx_api.py serve"
    echo       Waiting 12s for service to connect WeChat ...
    timeout /t 12 /nobreak >nul
)

echo [2/3] Starting bridge monitor ...
start "wx-bridge" cmd /k "cd /d %~dp0 && set PYTHONIOENCODING=utf-8 && "%PYEXE%" bridge.py"

echo [3/3] Starting window watchdog ...
start "wx-watchdog" cmd /k "cd /d %~dp0 && set PYTHONIOENCODING=utf-8 && "%PYEXE%" window_watchdog.py"

echo.
echo Done. Three windows are running.
echo Close the windows to stop. Logs in wechat_bridge\logs\
pause
