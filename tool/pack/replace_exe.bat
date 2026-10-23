@echo off
rem ============================================================
rem  替换被占用的 exe（服务停止后运行）：
rem    server\app_new.exe   -> server\app.exe
rem    client\agent_new.exe -> client\agent.exe
rem  若不存在 _new 文件则无事发生。
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0..\.."
if exist "server\app_new.exe" (
    move /y "server\app.exe" "server\app_old_%date:~0,4%%date:~5,2%%date:~8,2%.exe" >nul 2>nul
    move /y "server\app_new.exe" "server\app.exe" >nul && echo [OK] app.exe 已替换
)
if exist "client\agent_new.exe" (
    move /y "client\agent.exe" "client\agent_old_%date:~0,4%%date:~5,2%%date:~8,2%.exe" >nul 2>nul
    move /y "client\agent_new.exe" "client\agent.exe" >nul && echo [OK] agent.exe 已替换
)
pause
