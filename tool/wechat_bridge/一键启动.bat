@echo off
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
    start "wx-serve" cmd /k "cd /d C:\Users\chenhao\Desktop\DeepChat\client\workspace\workspace\wechat_headless_api && set PYTHONIOENCODING=utf-8 && C:\Users\chenhao\anaconda3\python.exe wx_api.py serve"
    echo       Waiting 12s for service to connect WeChat ...
    timeout /t 12 /nobreak >nul
)

echo [2/3] Starting bridge monitor ...
start "wx-bridge" cmd /k "cd /d C:\Users\chenhao\Desktop\DeepChat\client\workspace\workspace\wechat_bridge && set PYTHONIOENCODING=utf-8 && C:\Users\chenhao\anaconda3\python.exe bridge.py"

echo [3/3] Starting window watchdog ...
start "wx-watchdog" cmd /k "cd /d C:\Users\chenhao\Desktop\DeepChat\client\workspace\workspace\wechat_bridge && set PYTHONIOENCODING=utf-8 && C:\Users\chenhao\anaconda3\python.exe window_watchdog.py"

echo.
echo Done. Three windows are running.
echo Close the windows to stop. Logs in wechat_bridge\logs\
pause
