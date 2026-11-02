@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [牛哞哞] 未找到 python，请先安装 Python 3.10+ 并加入 PATH。
    pause
    exit /b 1
)

python -c "import requests" >nul 2>nul
if errorlevel 1 (
    echo [牛哞哞] 缺少 requests，正在安装...
    python -m pip install requests
)

set PYTHONIOENCODING=utf-8
set CHAT_BOT_LOG=cowsay_bot.log

echo.
echo [牛哞哞] 正在连接 http://127.0.0.1:5050 ...
echo [牛哞哞] 先确保 app.py 已经在运行；Ctrl+C 可以让它下班。
echo.

python cowsay_bot.py %*
if errorlevel 1 pause
