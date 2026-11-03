@echo off
chcp 65001 >nul
echo [提示] vLLM 官方不支持原生 Windows（需要 Linux / WSL2）。
echo 建议二选一：
echo   A. 用 WSL2 装 vLLM，然后跑 tools/start_rewrite_vllm.sh
echo   B. 用 llama.cpp 的 Windows 版：双击 tools\start_rewrite_llama.bat
pause
