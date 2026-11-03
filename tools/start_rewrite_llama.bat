@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set PYTHONUTF8=1
if "%GGUF%"=="" set "GGUF=%USERPROFILE%\models\rewrite3-shorter-4B-Q8_0.gguf"
if "%PORT%"=="" set "PORT=8080"
if "%LLAMA_SERVER%"=="" set "LLAMA_SERVER=llama-server"
if not exist "!GGUF!" (
    echo [错误] 找不到 GGUF 模型：!GGUF!
    echo 先下载 rewrite3-shorter-4B-Q8_0.gguf，然后设置：
    echo    set GGUF=D:\models\rewrite3-shorter-4B-Q8_0.gguf
    pause
    exit /b 1
)
echo 启动改文模型（llama.cpp）：!GGUF!
echo 端口 !PORT!，llama-server 需在 PATH 里（或改 LLAMA_SERVER）
"!LLAMA_SERVER!" -m "!GGUF!" --host 0.0.0.0 --port !PORT! -c 32768 -ngl 99 --jinja --alias rewrite3-shorter-4B
pause
