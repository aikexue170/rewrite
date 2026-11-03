@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist config.json (
    echo [提示] 还没配置文件，先双击 setup_windows.bat
    pause
    exit /b 1
)
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py
) else (
    echo [提示] 没找到 .venv，用系统 Python 试一下
    python main.py
)
pause
