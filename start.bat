@echo off
title webot - One-Click Launcher
setlocal enabledelayedexpansion

:: Switch to the folder this script lives in.
::
:: `cd /d` cannot enter a UNC path (VMware/Hyper-V shared folders, \\server\share).
:: It fails silently, leaves CWD wherever the shell started (e.g. C:\Windows),
:: and `python desktop.py` then reports:
::     can't open file 'C:\Windows\desktop.py': [Errno 2] No such file...
:: `pushd` maps a temporary drive letter for UNC paths and switches there, so
:: use it, and popd on the way out to release that mapping.
pushd "%~dp0" 2>nul
if errorlevel 1 (
    echo [Error] Cannot enter the script folder:
    echo         %~dp0
    echo         If this is a network/shared folder, make sure it is reachable.
    pause
    exit /b 1
)

:: Try to set UTF-8 codepage (suppress errors if it fails)
chcp 65001 > nul 2>&1

echo ============================================
echo   webot - One-Click Launcher
echo   (Bot auto-starts via WebView2 window)
echo ============================================
echo.

:: Find Python - try multiple command names
set PYTHON=
for %%p in (python python3 py) do (
    if "!PYTHON!"=="" (
        %%p --version > nul 2>&1
        if not errorlevel 1 set PYTHON=%%p
    )
)

if "%PYTHON%"=="" (
    echo [Error] Python not found. Please install Python 3.10+
    echo         https://www.python.org/downloads/
    echo         Check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

:: Launch the desktop application
%PYTHON% desktop.py %*
set EXITCODE=%ERRORLEVEL%
popd 2>nul
pause
exit /b %EXITCODE%
