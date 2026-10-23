@echo off
rem ============================================================
rem  DeepChat Agent launcher
rem
rem  Usage:
rem    run.bat              open the control panel
rem    run.bat --console    run the Agent directly in THIS console
rem
rem  The exe is built with --noconsole, so double-clicking gives no feedback for
rem  a few seconds. This window therefore stays up until the panel reports that
rem  its window is actually drawn, so "this window closes" == "the GUI is ready".
rem
rem  ASCII-only on purpose: cmd parses .bat with the OEM codepage, and non-ASCII
rem  comments can break parsing (we hit that).  Line endings must be CRLF.
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 >nul
title DeepChat Agent
cd /d "%~dp0"

set "ARG=%~1"
if /i "%ARG%"=="--console" goto :console

rem ============================================================
rem  GUI path
rem ============================================================
:gui

rem ---- the control-panel module must sit next to us ----
if not exist "%~dp0control.py" goto :no_control_py

rem ---- is the panel ALREADY open? then there is nothing to wait for ----
rem  Judged by the ready-marker the panel writes once its window is up: the file
rem  holds "<pid> <ts>", so we only ask whether that pid is still alive.
rem  Do NOT use "port is listening": a service can well be running with no panel
rem  (started elsewhere, or the panel was closed), and then the user wants THIS
rem  panel. Using the port made run.bat exit silently and look like a crash.
if exist "%~dp0logs\panel_ready" (
    set "PANELPID="
    for /f "usebackq tokens=1" %%P in ("%~dp0logs\panel_ready") do if not defined PANELPID set "PANELPID=%%P"
    set "PANELALIVE=0"
    if defined PANELPID (
        rem  Do NOT trust the PID alone: after the panel exits Windows
        rem  reuses its PID (we saw PID 9200 become svchost.exe), so a
        rem  bare "does this PID exist?" check thought the panel was
        rem  still open and run.bat exited silently. Verify image name.
        set "PANELIMG="
        rem  tasklist /fo csv columns: "ImageName","PID",... -- column 1 is the image name
        for /f "usebackq tokens=1 delims=," %%I in (`tasklist /fi "pid eq !PANELPID!" /fo csv /nh 2^>nul`) do set "PANELIMG=%%~I"
        if /i "!PANELIMG!"=="app.exe"     set "PANELALIVE=1"
        if /i "!PANELIMG!"=="agent.exe"   set "PANELALIVE=1"
        if /i "!PANELIMG!"=="pythonw.exe" set "PANELALIVE=1"
        if /i "!PANELIMG!"=="python.exe"  set "PANELALIVE=1"
    )
    if "!PANELALIVE!"=="1" (
        echo   The DeepChat control panel is already open - nothing to do.
        echo   Look for the DeepChat window on your taskbar.
        echo   ^(Still nothing? delete "%~dp0logs\panel_ready" then run again.^)
        timeout /t 4 >nul
        exit /b 0
    )
    rem  Stale marker (process gone, or PID now belongs to something else)
    rem  -> drop it and start normally.
    del /q "%~dp0logs\panel_ready" >nul 2>nul
)

rem ---- pick what to launch ----
set "EXEPATH="
set "PYWEXE="
set "SCRIPTPATH="
if exist "%~dp0agent.exe" set "EXEPATH=%~dp0agent.exe"

if not defined EXEPATH (
    rem ---- source-only mode ----
    rem  Two things must hold, and both were broken before:
    rem    1) the dependencies are installed. Without them the panel cannot start
    rem       and this window just polls until it times out (we hit that);
    rem    2) launch through pythonw, NOT python. python.exe is a console-subsystem
    rem       program, so `start` on it always opens an extra black window;
    rem       pythonw.exe is the GUI build and shows nothing.
    rem  Probe pythonw first, then python (as a last resort we still launch, but a
    rem  console window will appear).
    set "PYWEXE="
    set "PIPCMD="
    for %%D in ("%~dp0..\.backups\venv_pack\Scripts") do (
        if not defined PYWEXE if exist "%%~Dpythonw.exe" (
            "%%~Dpythonw.exe" -c "import flask,flask_cors,chardet,requests,pystray" >nul 2>nul
            if not errorlevel 1 (
                set "PYWEXE=%%~Dpythonw.exe"
                set "PIPCMD=%%~Dpython.exe"
            )
        )
        if not defined PYWEXE if not defined PIPCMD if exist "%%~Dpython.exe" (
            "%%~Dpython.exe" -c "import flask,flask_cors,chardet,requests,pystray" >nul 2>nul
            if not errorlevel 1 set "PIPCMD=%%~Dpython.exe"
        )
    )
    if not defined PYWEXE (
        pythonw -c "import flask,flask_cors,chardet,requests,pystray" >nul 2>nul
        if not errorlevel 1 set "PYWEXE=pythonw"
    )
    if not defined PYWEXE if not defined PIPCMD (
        python -c "import flask,flask_cors,chardet,requests,pystray" >nul 2>nul
        if not errorlevel 1 set "PIPCMD=python"
    )
    if not defined PYWEXE if not defined PIPCMD (
        echo   Installing Python dependencies ^(first run needs network, about 1 min^)...
        python -m pip install -q flask flask-cors chardet requests pystray -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 30 --disable-pip-version-check
        pythonw -c "import flask,flask_cors,chardet,requests,pystray" >nul 2>nul
        if not errorlevel 1 set "PYWEXE=pythonw"
        if not defined PYWEXE (
            python -c "import flask,flask_cors,chardet,requests,pystray" >nul 2>nul
            if not errorlevel 1 set "PIPCMD=python"
        )
    )
    if not defined PYWEXE if not defined PIPCMD (
        echo.
        echo [ERROR] Python dependencies are missing and could not be installed.
        echo         Run manually:  python -m pip install flask flask-cors chardet requests pystray
        echo         Or download the agent.exe build instead ^(no Python needed^).
        echo.
        pause
        exit /b 1
    )
    if not defined PYWEXE set "PYWEXE=!PIPCMD!"
    set "SCRIPTPATH=%~dp0agent.py"
)

echo ============================================================
echo   DeepChat Agent
echo.
echo   Starting the control panel, please wait...
echo   This window closes by itself once the panel is up.
echo ============================================================
echo.

set "READY=%~dp0logs\panel_ready"
if exist "!READY!" del /q "!READY!" >nul 2>nul

if defined EXEPATH (
    start "" "!EXEPATH!" --control
) else (
    start "" "!PYWEXE!" "!SCRIPTPATH!" --control
)

set /a TRIES=0
:wait
set /a TRIES+=1
if exist "!READY!" goto :done
if !TRIES! GEQ 60 goto :timeout
set /a MOD=TRIES %% 2
if !MOD!==0 <nul set /p "=."
ping -n 2 127.0.0.1 >nul
goto :wait

:done
echo.
echo   Ready - the control panel is now open.
exit /b 0

:timeout
echo.
echo.
echo [WARN] The control panel did not report ready within 30 seconds.
echo        It may still be starting. Logs: "%~dp0logs"
echo.
pause
exit /b 1

rem ============================================================
rem  Console path: run the agent in this window (no panel)
rem ============================================================
:console
set "PYEXE="
set "VENVPY=%~dp0..\.backups\venv_pack\Scripts\python.exe"
if exist "!VENVPY!" (
    "!VENVPY!" -c "flask,flask_cors,chardet,requests" >nul 2>nul
    if not errorlevel 1 set "PYEXE=!VENVPY!"
)
if not defined PYEXE (
    python -c "flask,flask_cors,chardet,requests" >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
    echo   Installing Python dependencies ^(first run needs network, about 1 min^)...
    python -m pip install -q flask flask-cors chardet requests pystray -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 30 --disable-pip-version-check
    python -c "flask,flask_cors,chardet,requests" >nul 2>nul
    if errorlevel 1 (
        echo.
        echo [ERROR] Python dependencies are missing and could not be installed.
        echo         Run manually:  python -m pip install flask flask-cors chardet requests pystray
        echo.
        pause
        exit /b 1
    )
    set "PYEXE=python"
)

if exist "%~dp0agent.exe" (
    cls
    "%~dp0agent.exe" --serve
    goto :console_end
)

cd /d "%~dp0workspace"
if not exist node_modules\puppeteer-core (
    if exist node\npm.cmd (
        echo Installing web-fetch dependencies ^(about 1 min^)...
        call node\npm.cmd install puppeteer-extra puppeteer-extra-plugin-stealth puppeteer-core --no-audit --no-fund --registry=https://registry.npmmirror.com
    ) else (
        echo [WARN] node\npm.cmd not found; skipping npm install, web fetching will be unavailable
    )
)
cd /d "%~dp0"
cls
echo ============================================================
echo   DeepChat Agent starting on port 5001
echo   Keep this window open while using DeepChat.
echo ============================================================
echo.
"!PYEXE!" agent.py --serve

:console_end
echo.
echo [agent exited] press any key to close this window...
pause >nul

rem ============================================================
rem  control.py missing
rem  NOTE: written as a goto target on purpose. If this message sat inside an
rem  `if not exist ( ... )` block, an install path containing parentheses
rem  (e.g. "deepchat_client (1)") would close the block early and cmd would die
rem  with "<path> was unexpected at this time." -- we hit exactly that.
rem ============================================================
:no_control_py
echo.
echo [ERROR] control.py not found next to this script:
echo         "%~dp0control.py"
echo.
echo         The control panel cannot start. This usually means only part
echo         of the project was copied - control.py must sit beside
echo         run.bat.
echo.
pause
exit /b 1
