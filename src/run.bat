@echo off
title Bamboozle - 3D Printer Monitor
cd /d "%~dp0"

:: Try the Python Manager install location first (most likely on this system)
set "PYPATH=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if exist "%PYPATH%" (
    "%PYPATH%" -m bamboozle.main
    goto :end
)

:: Try py launcher
py --version >nul 2>&1
if %errorlevel% equ 0 (
    py -m bamboozle.main
    goto :end
)

:: Try python from PATH (skip WindowsApps stubs)
for /f "delims=" %%i in ('python -c "import sys; print(sys.executable)" 2^>nul') do (
    "%%i" -m bamboozle.main
    goto :end
)

echo.
echo Python not found. Please install Python 3.10+ and add it to PATH.
echo Download from https://www.python.org/downloads/
echo.

:end
pause
