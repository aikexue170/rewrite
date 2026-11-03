@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if "%JEV_MODEL_DIR%"=="" (
    echo [错误] 先设置 jev 模型目录，例如：
    echo    set JEV_MODEL_DIR=D:\models\jev-0.8B-1007-bf16
    pause
    exit /b 1
)
if "%PYTHON%"=="" set "PYTHON=python"
if "%PORT%"=="" set "PORT=8885"
if "%DEVICE%"=="" set "DEVICE=cuda:0"
"%PYTHON%" "%~dp0serve_jev.py" --model-dir "%JEV_MODEL_DIR%" --host 0.0.0.0 --port %PORT% --device %DEVICE%
pause
