"""配置：项目根目录一个 config.json 搞定。

用户只需要复制 config.example.json 为 config.json，填云端模型的 key/url/model，
再确认改文模型和 jevnovel 的地址即可。
"""
import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("REWRITE_ROUTER_CONFIG") or (BASE_DIR / "config.json"))
EXAMPLE_PATH = BASE_DIR / "config.example.json"

PLACEHOLDER_KEYS = {"", "sk-你的key", "sk-xxxxxxxxxxxxxxxx", "填入你的key"}

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8011,
    "cloud": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-v4-pro",   # 只在请求没带 model 时兜底
        "forward_params": None,   # null = 除 messages/model/stream 外全部原样透传
        "min_max_tokens": 2048,
        "timeout_s": 600,
    },
    "rewrite": {
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key": "",
        "model": "rewrite3-shorter-4B",
        "instruction_file": "INSTRUCTION.txt",
        "temperature": 0.9,
        "top_p": 0.95,
        "repetition_penalty": 1.05,
        "max_tokens": 6144,
        "timeout_s": 900,
    },
    "jev": {
        "base_url": "http://127.0.0.1:8885",
        "timeout_s": 120,
        "chunk_chars": 300,
        "batch_units": 120,
    },
    "pipeline": {
        "max_source_chars": 3000,
        "fallback_to_original": True,
        "body_after_cot": True,       # 思维链全部出完再吐正文
        "rewrite_cot_header": "〔rewrite3思维链〕",
    },
    "protection": {
        "enabled": True,              # 前端卡结构保护：只改纯正文
        "min_prose_ratio": 0.15,      # 正文占比低于这个就整条不改
        "max_segments": 4,            # 正文被结构块切超过这么多段就整条不改
        "min_rewrite_chars": 60,      # 短于这个的正文段直接原样保留
        "note_in_reasoning": True,    # 跳过时在思维链里留一句说明
    },
    "debug": {
        "dump": False,             # true = 把最近一次改文的原文/切分写到 debug/last_plan.txt
    },
    "sse": {
        "heartbeat_interval_s": 5,
        "chunk_size": 8,
        "chunk_interval_s": 0.005,   # 正文打字机节奏（秒/块）
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        if EXAMPLE_PATH.exists():
            CONFIG_PATH.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"已生成配置文件：{CONFIG_PATH}")
            print("请打开它填写云端模型的 api_key / base_url / model 后重新启动。")
            sys.exit(0)
        raise SystemExit(f"找不到配置文件：{CONFIG_PATH}")
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"配置文件读取失败（{CONFIG_PATH}）：{exc}")
    return _deep_merge(DEFAULTS, data)


CFG = load_config()


def cloud_key_ok() -> bool:
    return (CFG["cloud"].get("api_key") or "").strip() not in PLACEHOLDER_KEYS


def resolve_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (BASE_DIR / path)
