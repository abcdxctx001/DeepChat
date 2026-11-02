@echo off
rem DeepCoder launcher: opens workspace in current dir, optional target file
rem usage: deepcoder [file] [--port N] [--no-browser]
cd workspace
where python >nul 2>nul
if %errorlevel%==0 (
    start "DeepCoder" python "%~dp0server.py" %*
    exit /b 0
)
where py >nul 2>nul
if %errorlevel%==0 (
    start "DeepCoder" py -3 "%~dp0server.py" %*
    exit /b 0
)
echo [DeepCoder] Python not found. Please install Python 3.8+ (python.org).
pause
