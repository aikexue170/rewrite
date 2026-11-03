---
name: shubing-router
description: 薯饼路由器（rewrite router）— 把 SillyTavern / OpenAI 兼容请求转给云端模型，再用本地 rewrite3 改文模型重写正文，并把云端原生思维链 + 改文思维链一起返回。Use this skill when the user wants to deploy, configure, run, or troubleshoot this repository (rewrite3, jevnovel scoring, SillyTavern 改文, de-AIGC proxy, llama.cpp/vLLM 本地服务启动).
---

# 薯饼路由器 · 部署说明（给 AI agent）

## 目标

把本仓库跑起来，形成这条链路并验证通过：

```
客户端(酒馆/curl) → 路由:8011 → 云端模型(OpenAI 兼容)
                                  ├─ jevnovel 打分:8885
                                  └─ rewrite3 改文:8080 (llama.cpp 或 vLLM)
```

完成后要给用户两个地址：
- 体验前端：`http://127.0.0.1:8011/`
- 酒馆/客户端：`http://127.0.0.1:8011/v1`（API Key 随便填）

## 前置检查（先做，不要跳）

```bash
python3 --version            # 需要 >= 3.10
git --version
nvidia-smi || rocm-smi       # 有 GPU 用 GPU；纯 CPU 也能跑但很慢
df -h                        # 至少留 15G 给模型
curl -sI https://huggingface.co | head -1   # 能访问 HF
```

显存需求：改文模型 Q4_K_M≈2.6G / Q8≈4.6G / FP8≈5.5G，jev 0.8B 带开销≈2G。
8G 卡可爽玩（Q4 或 Q8+小上下文，jev 同卡）；6G 属极限（Q4 + `-c 4096`，jev 同卡）；24G 随便挑。
端口检查：8011 / 8080 / 8885 不能被占。

## 步骤 1：装路由

```bash
git clone https://github.com/aikexue170/rewrite.git
cd rewrite
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json
```

Windows 用 `setup_windows.bat`（自动建 venv、装依赖、生成 config.json）。

## 步骤 2：下载模型

```bash
pip install -U huggingface_hub

# 改文模型（GGUF 省事）：q4km 最省显存，q8 平衡
hf download aikexue170/rewrite3-4b-shorter-q8   --local-dir models/rewrite3-q8
# 或者 NVIDIA vLLM 路线：
# hf download aikexue170/rewrite3-4b-shorter-fp8 --local-dir models/rewrite3-fp8

# 打分模型（目录里必须有 infer.py，serve_jev.py 靠它加载）
hf download aikexue170/jev-novel-3-0.8b-fp8 --local-dir models/jev-0.8b
```

模型对应关系（精度 → 链接在 README 的「需要准备的模型」表）：

| 用途 | 推荐下载 | 显存 |
|---|---|---|
| 改文（llama.cpp） | `rewrite3-4b-shorter-q8` 或 `-q4km` | 5G / 2.6G |
| 改文（vLLM） | `rewrite3-4b-shorter-fp8` / `-nvfp4` | 6G / 4G |
| 打分 | `jev-novel-3-0.8b-fp8` | 2G |

jev 服务需要单独的 Python 环境（torch + transformers）：

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu124   # NVIDIA；AMD 用 rocm 源
pip install -r tools/requirements-jev.txt
```

## 步骤 3：填 config.json

```jsonc
{
  "host": "0.0.0.0", "port": 8011,
  "cloud": {
    "base_url": "https://api.deepseek.com/v1",   // 任意 OpenAI 兼容服务
    "api_key": "<向用户要，不要提交到仓库>",
    "model": "deepseek-v4-pro",                  // 兜底模型；请求里带的 model 会透传
    "min_max_tokens": 2048                        // 太小会截断云端正文
  },
  "rewrite": {
    "base_url": "http://127.0.0.1:8080/v1",      // llama.cpp/vLLM 的 OpenAI 兼容地址
    "model": "rewrite3-shorter-4B",              // 必须等于 --alias / --served-model-name
    "instruction_file": "INSTRUCTION.txt",
    "max_tokens": 6144
  },
  "jev":  { "base_url": "http://127.0.0.1:8885" },
  "pipeline": { "body_after_cot": true, "rewrite_cot_header": "〔rewrite3思维链〕" },
  "protection": { "enabled": true },
  "debug": { "dump": false }
}
```

必须让用户提供：`cloud.api_key`（云端模型 key）。其余字段一般默认即可。
**本仓库的 `config.json` 已在 .gitignore 里，绝不要把真实 key 提交/写进任何会推送的文件。**

## 步骤 4：拉起三个服务

```bash
# 1) 改文模型（二选一）
GGUF=./models/rewrite3-q8/*.gguf PORT=8080 tools/start_rewrite_llama.sh
# 或
MODEL_DIR=./models/rewrite3-fp8 PORT=8080 tools/start_rewrite_vllm.sh

# 2) jevnovel 打分
JEV_MODEL_DIR=./models/jev-0.8b PORT=8885 tools/start_jev.sh

# 3) 路由（前台运行，看横幅）
.venv/bin/python main.py
```

Windows 对应：`tools\start_rewrite_llama.bat`、`tools\start_jev.bat`、`start_router.bat`。
vLLM 不支持原生 Windows，Windows 上必须走 llama.cpp 或 WSL2。

> 只想体验「直接改文」、不接云端也行：路由照样能启动，前端「直接改文」只依赖 rewrite + jev。

## 步骤 5：验证（每步都要过）

```bash
# 三个下游都要 ok
curl -s http://127.0.0.1:8011/health
# 云端整条链路：应能收到 reasoning_content 和 content
curl -N -X POST http://127.0.0.1:8011/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-flash","stream":true,"messages":[{"role":"user","content":"写一句话测试"}]}'
# 直接改文（不需要云端）：应返回 marked/reasoning/content 事件
curl -N -X POST http://127.0.0.1:8011/api/rewrite \
  -H 'Content-Type: application/json' \
  -d '{"text":"雨丝斜斜地落下，像一层薄薄的纱，将整座城市笼罩在一片朦胧之中。"}'
```

通过标准：
1. `/health` 里 `cloud.ok / rewrite.ok / jev.ok` 全是 true（jev 挂了也能跑，只是分数变【NA】）。
2. `/v1/chat/completions` 的事件顺序是：云端 reasoning → `〔rewrite3思维链〕` → 改文 reasoning → content。
3. `/api/rewrite` 有 `marked` 事件，content 是改写后的正文。

## 步骤 6：告诉用户怎么接

- 浏览器体验：`http://127.0.0.1:8011/`
- 酒馆：连接类型 Chat Completion、Source Custom (OpenAI-compatible)、Endpoint `http://127.0.0.1:8011/v1`、API Key 随便填、模型从下拉里选（列表来自云端 `/v1/models`）。
- 手机/别机：`http://<局域网IP>:8011/v1`，需要放行防火墙 8011。

## 排错表

| 症状 | 原因 | 处理 |
|---|---|---|
| 云端 401/403 | key 不对 | 让用户重新给 `cloud.api_key` |
| 改文 404 / model not found | config 里 rewrite.model 和服务名不一致 | 改成启动参数里的 `--alias` / `--served-model-name` |
| jev 分数全是【NA】 | jev 服务没起或地址不对 | 起 `tools/serve_jev.py`，`curl :8885/health` |
| 正文迟迟不出来 | 整篇模式 + 改文 CoT 很长 | 正常现象；可调 `pipeline.body_after_cot=false` 或减小 `max_source_chars` |
| 显存 OOM | 精度太高 | 换 Q4_K_M / nvfp4；jev 可 CPU |
| 端口被占 | 8011/8080/8885 冲突 | 改 config/bat 里的端口 |
| Windows 控制台乱码 | 编码 | 用仓库里的 `.bat`（已 chcp 65001 + PYTHONUTF8=1） |
| 前端页面打不开 | 路由没起或端口不对 | `curl :8011/health` |

## 给 agent 的约束

- 不要修改 `app/sentences.py`：它必须和 jev 训练时的分句规则一致，改了分数口径就错。
- 不要默认关掉 `protection`：前端卡的状态栏/HTML/变量块靠它保护。
- 不要把 `config.json`、`debug/`、`.venv/` 提交到 git。
- 没有用户明确要求，不要把这个服务暴露到公网（无鉴权，会盗用用户的云端 key 额度）。
- 先验证 `/health` 三项全绿，再做端到端测试。
