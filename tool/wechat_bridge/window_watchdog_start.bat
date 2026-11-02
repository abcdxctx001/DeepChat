@echo off
rem 微信窗口尺寸守护启动脚本（供定时监控任务使用）
chcp 65001 >nul
cd /d "C:\Users\chenhao\Desktop\DeepChat\client\workspace\workspace\wechat_bridge"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
C:\Users\chenhao\anaconda3\python.exe window_watchdog.py
