@echo off
rem 微信消息桥接启动脚本（供定时监控任务使用）
chcp 65001 >nul
cd /d "C:\Users\chenhao\Desktop\DeepChat\client\workspace\workspace\wechat_bridge"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
C:\Users\chenhao\anaconda3\python.exe bridge.py
