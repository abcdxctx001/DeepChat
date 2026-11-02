@echo off
chcp 65001 >nul
title 虚拟炒股模拟器
cd /d "%~dp0"
echo 正在启动虚拟炒股模拟器...
python stock_sim.py
if errorlevel 1 (
  echo.
  echo [错误] 启动失败，请确认已安装 Python 3 并勾选 "Add Python to PATH"
  pause
)
