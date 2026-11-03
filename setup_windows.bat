@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
echo ==========================================
echo   薯饼路由器 - Windows 环境安装
echo ==========================================
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% --version
if errorlevel 1 (
    echo [错误] 没找到 Python，请先安装 Python 3.10+ 并勾选 "Add Python to PATH"
    pause
    exit /b 1
)
if not exist .venv (
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败
        pause
        exit /b 1
    )
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)
if not exist config.json (
    copy config.example.json config.json >nul
    echo 已生成 config.json，请先填写 cloud.api_key / base_url / model
)
echo.
echo 安装完成。接下来：
echo   1. 编辑 config.json（填云端模型 key）
echo   2. 启动改文模型：tools\start_rewrite_llama.bat
echo   3. 装 jevnovel 依赖（torch+transformers，见 README），然后 tools\start_jev.bat
echo   4. 启动路由：start_router.bat
pause
