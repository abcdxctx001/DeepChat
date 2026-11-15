@echo off
rem ============================================================
rem  DeepChat Server launcher
rem
rem  Usage:
rem    run.bat              open the control panel
rem    run.bat --console    run the Server directly in THIS console
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
title DeepChat Server
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
if exist "%~dp0app.exe" set "EXEPATH=%~dp0app.exe"

if defined EXEPATH goto :have_exe

rem ============================================================
rem  source-only mode
rem
rem  FIX 2026-11-11 (bug 10: run.bat picked the wrong Python):
rem    The old order was  ..\.backups\venv_pack  ->  pythonw/python on PATH.
rem    On the user's machine the python on PATH is
rem    D:\emacs-26.3-x86_64\bin\python.exe (Emacs' own, no pip at all),
rem    so double-clicking run.bat failed with "No module named pip".
rem    Now the bundled interpreter  %~dp0workspace\python\python.exe  is
rem    ALWAYS tried first; only when it is absent do we fall back to the
rem    dev venv and finally to PATH.
rem
rem  Two older constraints stay valid:
rem    1) dependencies must be importable, otherwise the panel never starts
rem       and this window just polls until it times out;
rem    2) prefer pythonw to launch (python.exe is a console app, so `start`
rem       on it opens an extra black window).
rem ============================================================
set "PIPCMD="
set "PYWEXE="

set "CAND_PY=%~dp0workspace\python\python.exe"
set "CAND_PYW=%~dp0workspace\python\pythonw.exe"
if exist "%~dp0workspace\python\python.exe" goto :probe_env
set "CAND_PY=%~dp0..\.backups\venv_pack\Scripts\python.exe"
set "CAND_PYW=%~dp0..\.backups\venv_pack\Scripts\pythonw.exe"
if exist "%~dp0..\.backups\venv_pack\Scripts\python.exe" goto :probe_env
rem last resort: python on PATH (this may well be the emacs one)
set "CAND_PY="
set "CAND_PYW="
goto :probe_env

:probe_env
if not defined CAND_PY goto :probe_path
"!CAND_PY!" -c "import flask,flask_cors,chardet,requests,openai,pystray" >nul 2>nul
if errorlevel 1 goto :deps_install
set "PIPCMD=!CAND_PY!"
if exist "!CAND_PYW!" (set "PYWEXE=!CAND_PYW!") else (set "PYWEXE=!CAND_PY!")
goto :py_ready

rem ---- install dependencies into the chosen interpreter ----
rem  FIX 2026-11-12 (bug B): a bundled python may be an "embeddable" package,
rem  which ships WITHOUT pip -- so "python -m pip install" dies with
rem  "No module named pip", the re-probe fails, and the user is told
rem  "No usable Python found" even though python itself worked fine.
rem  Probe pip first; try ensurepip and a get-pip.py sitting next to the
rem  interpreter; only then give up with an accurate message.
:deps_install
"!CAND_PY!" -m pip --version >nul 2>nul
if not errorlevel 1 goto :deps_install_do
"!CAND_PY!" -m ensurepip --upgrade >nul 2>nul
"!CAND_PY!" -m pip --version >nul 2>nul
if not errorlevel 1 goto :deps_install_do
set "PYDIR=%~dp0workspace\python\"
for %%F in ("!CAND_PY!") do set "PYDIR=%%~dpF"
if exist "!PYDIR!get-pip.py" (
    echo   Bootstrapping pip from "!PYDIR!get-pip.py" ...
    "!CAND_PY!" "!PYDIR!get-pip.py" --no-warn-script-location >nul 2>nul
)
"!CAND_PY!" -m pip --version >nul 2>nul
if errorlevel 1 goto :deps_no_pip

:deps_install_do
echo   Installing Python dependencies into:
echo     !CAND_PY!
echo   (first run needs network, about 1 min)
"!CAND_PY!" -m pip install -q flask flask-cors chardet requests "openai==2.2.0" pystray -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 30 --disable-pip-version-check
"!CAND_PY!" -c "import flask,flask_cors,chardet,requests,openai,pystray" >nul 2>nul
if errorlevel 1 goto :deps_missing
set "PIPCMD=!CAND_PY!"
if exist "!CAND_PYW!" (set "PYWEXE=!CAND_PYW!") else (set "PYWEXE=!CAND_PY!")
goto :py_ready

rem  FIX 2026-11-12 (bug A): the PATH fallback used to jump straight to
rem  :deps_missing when the imports failed. But "the imports failed" can also
rem  mean "there IS a python here, it just lacks the packages" -- telling the
rem  user "No usable Python found" was flat wrong, and it never tried to
rem  install anything. Now: if a working interpreter exists on PATH, go
rem  through the same install path as the bundled one.
:probe_path
rem neither bundled nor venv python: fall back to PATH
pythonw -c "import flask,flask_cors,chardet,requests,openai,pystray" >nul 2>nul
if not errorlevel 1 (
    set "PYWEXE=pythonw"
    set "PIPCMD=python"
    goto :py_ready
)
python -c "import flask,flask_cors,chardet,requests,openai,pystray" >nul 2>nul
if not errorlevel 1 (
    set "PIPCMD=python"
    set "PYWEXE=python"
    goto :py_ready
)
rem imports failed -- is there an interpreter at all?
python -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "CAND_PY=python"
    set "CAND_PYW=pythonw"
    rem pythonw may not exist; deps_install falls back to CAND_PY when missing
    if not exist "%~dp0workspace\python\pythonw.exe" set "CAND_PYW="
    goto :deps_install
)
pythonw -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "CAND_PY=pythonw"
    set "CAND_PYW=pythonw"
    goto :deps_install
)
goto :deps_missing

rem  FIX 2026-11-12 (bug B): accurate message -- we DID find python, it just
rem  has no pip. Telling the user "no python found" sent them hunting for the
rem  wrong problem.
:deps_no_pip
echo.
echo [ERROR] Found Python but it has NO pip, so dependencies cannot be
echo         installed automatically:
echo           !CAND_PY!
echo.
echo         This is normal for an "embeddable" Python package. Options:
echo           1) put get-pip.py next to that python.exe and run again, or
echo           2) use the full Python installer, or
echo           3) download the app.exe build (no Python needed).
echo.
pause
exit /b 1

:deps_missing
echo.
echo [ERROR] No usable Python found for the source build.
echo         Looked for (in this order):
echo           1) "%~dp0workspace\python\python.exe"
echo           2) "%~dp0..\.backups\venv_pack\Scripts\python.exe"
echo           3) pythonw / python on PATH
echo.
echo         Install the bundled runtime, or run manually:
echo           python -m pip install flask flask-cors chardet requests "openai==2.2.0" pystray
echo         Or download the app.exe build instead ^(no Python needed^).
echo.
pause
exit /b 1

:py_ready
set "SCRIPTPATH=%~dp0app.py"
goto :after_py

:have_exe
set "SCRIPTPATH="

:after_py

echo ============================================================
echo   DeepChat Server
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
rem  Console path: run the server in this window (no panel)
rem ============================================================
:console
if exist "%~dp0app.exe" (
    start "" http://localhost:5000
    cls
    "%~dp0app.exe" --serve
    goto :eof
)
rem FIX 2026-11-11 (bug 10): console path must use the same interpreter as the
rem   panel path -- never a bare "py"/"python" from PATH.
rem FIX 2026-11-13: it also ran "pip install" unconditionally on every start
rem   (about a minute, with network) even when everything was already there.
rem   Probe first, install only when something is missing; and bootstrap pip
rem   when the interpreter has none.
set "PYCON=%~dp0workspace\python\python.exe"
if not exist "!PYCON!" set "PYCON="
if not defined PYCON (
    where py >nul 2>nul
    if not errorlevel 1 set "PYCON=py"
)
if not defined PYCON (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYCON=python"
)
if not defined PYCON (
    echo.
    echo [ERROR] No usable Python found for --console mode.
    echo         Install the bundled runtime, or run the app.exe build instead.
    echo.
    pause
    exit /b 1
)
"!PYCON!" -c "import flask,flask_cors,chardet,requests,openai,pystray" >nul 2>nul
if errorlevel 1 (
    echo   Installing Python dependencies ^(first run needs network, about 1 min^)...
    "!PYCON!" -m pip --version >nul 2>nul
    if errorlevel 1 "!PYCON!" -m ensurepip --upgrade >nul 2>nul
    "!PYCON!" -m pip install flask flask-cors openai chardet requests pystray PyPDF2 python-docx openpyxl python-pptx -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 30 --disable-pip-version-check
    "!PYCON!" -c "import flask,flask_cors,chardet,requests,openai,pystray" >nul 2>nul
    if errorlevel 1 (
        echo.
        echo [ERROR] Python dependencies are missing and could not be installed.
        echo         Run manually:  "!PYCON!" -m pip install flask flask-cors openai chardet requests pystray
        echo.
        pause
        exit /b 1
    )
)
start "" http://localhost:5000
cls
"!PYCON!" app.py --serve
goto :eof

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
