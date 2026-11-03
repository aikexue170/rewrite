"""改文模型客户端（vLLM / llama.cpp 等 OpenAI 兼容服务）。

输入 = INSTRUCTION.txt 全文 + 带【】标记的原文（中间不加分隔，和训练/评测脚本一致）。
输出 = 改文思维链 + `</think>` + 改写正文。思维链增量实时转发，正文增量直接流给前端。

服务端不需要开 reasoning parser：这里自己按 `</think>` 增量切。如果服务端已经把
思维链拆到 reasoning_content 字段里（llama.cpp --jinja 常见），也兼容。
"""
import json
import logging
from typing import Awaitable, Callable

import httpx

from .config import CFG, resolve_path

logger = logging.getLogger(__name__)

THINK_END = "</think>"
THINK_START = "<think>"

_instruction_cache: str | None = None


class RewriteError(RuntimeError):
    pass


def load_instruction() -> str:
    global _instruction_cache
    if _instruction_cache is None:
        path = resolve_path(CFG["rewrite"].get("instruction_file") or "INSTRUCTION.txt")
        try:
            _instruction_cache = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RewriteError(f"读不到改文指令文件 {path}：{exc}") from exc
    return _instruction_cache


def _headers(api_key: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def stream_rewrite(
    marked_text: str,
    on_reasoning: Callable[[str], Awaitable[None]],
    on_content: Callable[[str], Awaitable[None]],
) -> str:
    """流式改文。Returns: 完整改写正文。"""
    cfg = CFG["rewrite"]
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    prompt = load_instruction() + marked_text
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "temperature": cfg.get("temperature", 0.9),
        "top_p": cfg.get("top_p", 0.95),
        "repetition_penalty": cfg.get("repetition_penalty", 1.05),
        "max_tokens": cfg.get("max_tokens", 6144),
    }
    timeout = httpx.Timeout(float(cfg.get("timeout_s") or 900), connect=15)

    body_parts: list[str] = []
    pending = ""            # 还没遇到 </think> 时先攒着，避免把半截标签当正文发出去
    split_done = False      # 已经切出正文
    had_reasoning = False   # 服务端把思维链拆到 reasoning_content 了

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream("POST", url, json=payload,
                                     headers=_headers(cfg.get("api_key") or "")) as resp:
                if resp.status_code >= 400:
                    raw = (await resp.aread()).decode("utf-8", "replace")
                    raise RewriteError(f"改文模型返回 {resp.status_code}：{raw[:500]}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        if data == "[DONE]":
                            break
                        continue
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("error"):
                        raise RewriteError(f"改文模型报错：{str(obj['error'])[:300]}")
                    for choice in obj.get("choices") or []:
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(reasoning, str) and reasoning:
                            had_reasoning = True
                            await on_reasoning(reasoning)
                        content = delta.get("content")
                        if not isinstance(content, str) or not content:
                            continue
                        if had_reasoning:
                            # 服务端已经拆好思维链了，content 就是正文，直接流出去
                            body_parts.append(content)
                            await on_content(content)
                            continue
                        if split_done:
                            body_parts.append(content)
                            await on_content(content)
                            continue
                        pending += content
                        idx = pending.find(THINK_END)
                        if idx < 0:
                            continue
                        cot = pending[:idx]
                        if THINK_START in cot:
                            cot = cot.split(THINK_START)[-1]
                        cot = cot.strip("\n")
                        body = pending[idx + len(THINK_END):]
                        split_done = True
                        pending = ""
                        if cot:
                            await on_reasoning(cot)
                        if body:
                            body = body.lstrip("\n")
                            if body:
                                body_parts.append(body)
                                await on_content(body)
        except httpx.HTTPError as exc:
            raise RewriteError(f"改文模型请求失败：{exc}") from exc

    # 模型没吐 </think>（异常情况）：把攒着的内容当正文兜底
    if not split_done and pending.strip():
        logger.warning("改文模型输出里没有 </think>，按纯正文处理（%d 字符）", len(pending))
        body_parts.append(pending)
        await on_content(pending)

    body = "".join(body_parts).strip()
    if had_reasoning and not split_done and not body:
        raise RewriteError("改文模型只返回了思维链，没有正文（max_tokens 可能给小了）")
    return body


async def health(timeout: float = 3.0) -> tuple[bool, str]:
    url = CFG["rewrite"]["base_url"].rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=_headers(CFG["rewrite"].get("api_key") or ""))
        if resp.status_code < 400:
            return True, f"HTTP {resp.status_code}"
        return False, f"HTTP {resp.status_code}"
    except httpx.HTTPError as exc:
        return False, str(exc)[:80]
