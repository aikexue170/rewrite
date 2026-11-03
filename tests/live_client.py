#!/usr/bin/env python3

# -*- coding: utf-8 -*-
"""真实链路冒烟客户端：像 SillyTavern 一样给路由发一条请求，打印流式事件和时间线。

用法：
  .venv/bin/python tests/live_client.py [base_url] [max_tokens] [提示词]
"""

import sys
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import json
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
MAX_TOKENS = int(sys.argv[2]) if len(sys.argv) > 2 else 1200
PROMPT = sys.argv[3] if len(sys.argv) > 3 else (
    "写一段雨夜重逢：男主在便利店门口遇到五年前不辞而别的前女友，"
    "两人有一瞬间都没说话。600字左右。")

t0 = time.time()
first_reasoning = first_header = first_content = None
reasoning, content = [], []
last_kind = None

with httpx.Client(timeout=900) as client:
    with client.stream(
        "POST",
        f"{BASE}/v1/chat/completions",
        json={
            "model": "deepseek-v4-pro",
            "stream": True,
            "temperature": 1.0,
            "top_p": 0.95,
            "max_tokens": MAX_TOKENS,
            "messages": [
                {"role": "system", "content": "你是一个中文网文作者，擅长都市情感，文笔细腻。"},
                {"role": "user", "content": PROMPT},
            ],
        },
    ) as resp:
        print("HTTP", resp.status_code, flush=True)
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            obj = json.loads(data)
            if "error" in obj:
                print("ERROR:", obj["error"], flush=True)
                continue
            delta = (obj.get("choices") or [{}])[0].get("delta") or {}
            now = time.time() - t0
            if delta.get("reasoning_content"):
                if first_reasoning is None:
                    first_reasoning = now
                if last_kind != "r":
                    print(f"\n[{now:6.1f}s] ---- 思维链开始 ----", flush=True)
                reasoning.append(delta["reasoning_content"])
                print(delta["reasoning_content"], end="", flush=True)
                last_kind = "r"
                if first_header is None and "<rewrite3思维链>" in "".join(reasoning[-4:]):
                    first_header = now
                    print(f"\n[{now:6.1f}s] ---- 进入改文思维链 ----", flush=True)
            if delta.get("content"):
                if first_content is None:
                    first_content = now
                    print(f"\n[{now:6.1f}s] ---- 正文开始 ----", flush=True)
                content.append(delta["content"])
                print(delta["content"], end="", flush=True)
                last_kind = "c"

print("\n")
print("=" * 60)
print(f"首字(思维链) : {first_reasoning:.1f}s" if first_reasoning else "无思维链")
print(f"改文思维链   : {first_header:.1f}s" if first_header else "无 <rewrite3思维链>")
print(f"首字(正文)   : {first_content:.1f}s" if first_content else "无正文")
print(f"总耗时       : {time.time() - t0:.1f}s")
print(f"思维链字数   : {len(''.join(reasoning))}")
print(f"正文字数     : {len(''.join(content))}")
