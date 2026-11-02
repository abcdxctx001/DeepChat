@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [Chat5050] 未找到 python，请先安装 Python 3.10+ 并加入 PATH。
    pause
    exit /b 1
)

python -c "import flask" >nul 2>nul
if errorlevel 1 (
    echo [Chat5050] 缺少 Flask，正在安装依赖...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [Chat5050] 依赖安装失败，请手动运行：
        echo     python -m pip install -r requirements.txt
        pause
        exit /b 1
    )
)

echo.
echo [Chat5050] 正在启动，浏览器访问 http://127.0.0.1:5050
echo.
python app.py
if errorlevel 1 pause
