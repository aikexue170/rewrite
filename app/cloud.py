"""云端模型客户端：普通 OpenAI 兼容流式调用。

按用户要的体验：云端模型的思维链实时转发给前端（ST 的灰色思考框），
正文一律**不转发**，只在后台攒起来，等整个响应结束后交给改文流水线。
"""
import json
import logging
import re
from typing import Awaitable, Callable

import httpx

from .config import CFG

logger = logging.getLogger(__name__)

THINK_RE = re.compile(r"<think>(.*?)</think>", re.S)


class CloudError(RuntimeError):
    pass


def _headers(api_key: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def list_models(timeout: float = 8.0) -> list[dict]:
    """透传云端模型的 /models 列表（不要自己维护一份）。"""
    cfg = CFG["cloud"]
    url = cfg["base_url"].rstrip("/") + "/models"
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5)) as client:
        try:
            resp = await client.get(url, headers=_headers(cfg.get("api_key") or ""))
        except httpx.HTTPError as exc:
            raise CloudError(f"拉云端模型列表失败：{exc}") from exc
    if resp.status_code >= 400:
        raise CloudError(f"云端模型列表返回 {resp.status_code}：{resp.text[:200]}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise CloudError(f"云端模型列表不是 JSON：{exc}") from exc
    raw = payload.get("data") if isinstance(payload, dict) else payload
    models = []
    for item in raw or []:
        if isinstance(item, dict) and item.get("id"):
            models.append({"id": str(item["id"]), "object": "model",
                           "owned_by": str(item.get("owned_by") or "cloud")})
        elif isinstance(item, str):
            models.append({"id": item, "object": "model", "owned_by": "cloud"})
    if not models:
        raise CloudError("云端模型列表是空的")
    return models


async def stream_cloud(
    messages: list[dict],
    forwarded_params: dict,
    on_reasoning: Callable[[str], Awaitable[None]],
    model: str | None = None,
) -> tuple[str, bool]:
    """流式调云端模型。

    Returns:
        (完整正文, 是否出现过原生思维链)
    """
    cfg = CFG["cloud"]
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload: dict = {"model": model or cfg["model"], "messages": messages, "stream": True}
    for key, value in (forwarded_params or {}).items():
        if value is not None:
            payload[key] = value

    body_parts: list[str] = []
    had_reasoning = False
    timeout = httpx.Timeout(float(cfg.get("timeout_s") or 600), connect=30)

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream("POST", url, json=payload,
                                     headers=_headers(cfg.get("api_key") or "")) as resp:
                if resp.status_code >= 400:
                    raw = (await resp.aread()).decode("utf-8", "replace")
                    raise CloudError(f"云端模型返回 {resp.status_code}：{raw[:500]}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("error"):
                        raise CloudError(f"云端模型报错：{str(obj['error'])[:300]}")
                    for choice in obj.get("choices") or []:
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(reasoning, str) and reasoning:
                            had_reasoning = True
                            await on_reasoning(reasoning)
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            body_parts.append(content)
        except httpx.HTTPError as exc:
            raise CloudError(f"云端模型请求失败：{exc}") from exc

    body = "".join(body_parts)

    # 兜底：个别供应商不给 reasoning 字段，而是把思考塞在 content 的 <think> 里
    if not had_reasoning:
        match = THINK_RE.search(body)
        if match:
            await on_reasoning(match.group(1).strip())
            body = (body[:match.start()] + body[match.end():]).strip()
            had_reasoning = True
            logger.info("云端思维链藏在正文 <think> 里，已拆出 %d 字符", len(match.group(1)))

    return body, had_reasoning


async def health(timeout: float = 5.0) -> tuple[bool, str]:
    """探测云端地址是否可达（只打 /models，不消耗额度）。"""
    cfg = CFG["cloud"]
    url = cfg["base_url"].rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=_headers(cfg.get("api_key") or ""))
        if resp.status_code < 400:
            return True, f"HTTP {resp.status_code}"
        if resp.status_code in (401, 403):
            return False, f"HTTP {resp.status_code}（key 可能不对）"
        return False, f"HTTP {resp.status_code}"
    except httpx.HTTPError as exc:
        return False, str(exc)[:80]
