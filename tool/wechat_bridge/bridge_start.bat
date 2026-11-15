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
rem 微信消息桥接启动脚本（供定时监控任务使用）
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
"%PYEXE%" bridge.py
