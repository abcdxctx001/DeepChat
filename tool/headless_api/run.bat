@echo off
chcp 65001 >nul
title DeepChat Headless API
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 未找到 python，请先安装 Python 3.8+ 并加入 PATH
    pause
    exit /b 1
)

python -c "import flask, requests" 2>nul
if errorlevel 1 (
    echo [INFO] 正在安装依赖: flask requests ...
    pip install flask requests
)

python headless_api.py
pause
