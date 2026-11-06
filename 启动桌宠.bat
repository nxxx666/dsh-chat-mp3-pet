@echo off
rem 一键启动：用 PATH 上的 Python（3.10+，需已装 PyQt6）
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found in PATH. Install Python 3.10+ first.
    pause
    exit /b 1
)
python setup_and_run.py
if errorlevel 1 pause
