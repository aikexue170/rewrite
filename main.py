#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""薯饼路由器：SillyTavern → 云端模型 + rewrite3 + jevnovel 的一条龙转发服务。

对 SillyTavern 暴露 OpenAI 兼容的 POST /v1/chat/completions：
- 把 ST 的请求原样转发给云端模型，它的原生思维链实时显示（灰色小字）
- 云端正文不显示，后台收完整
- 逐句用 jevnovel 0.8B 打分，拼成「原句【0.123】」
- 整篇交给 rewrite3 改文，先流式显示改文思维链（前面加 <rewrite3思维链>），再流式显示改写正文
- 前端卡的结构内容（HTML/状态栏/变量块）原样保留，只改纯正文
- 任何一环挂了，默认降级为直接输出云端原文，聊天不会断

浏览器打开 http://127.0.0.1:8011/ 是自带的改文体验前端（不用酒馆）：
- 直接改文：贴一段 AIGC 文本，实时看改文思维链和改写结果
- 云端生成 → 改文：给一句场景，云端先写，再自动改文

启动：python main.py
配置：同目录 config.json（一个文件搞定，见 config.example.json）
"""
import sys as _sys

for _stream in (_sys.stdout, _sys.stderr):        # Windows 控制台/重定向统一 UTF-8
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from app import cloud, jev, pipeline, rewrite
from app.config import BASE_DIR, CFG, CONFIG_PATH, cloud_key_ok
from app.sse import stream_completion

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("router")

app = FastAPI(title="薯饼路由器", version="0.2.0")

WEB_DIR = BASE_DIR / "web"
_INDEX_CACHE: str | None = None


def _index_html() -> str:
    global _INDEX_CACHE
    if _INDEX_CACHE is None:
        _INDEX_CACHE = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    return _INDEX_CACHE


_MODELS_CACHE: dict = {"at": 0.0, "data": None}
_MODELS_TTL = 60.0


async def _cloud_models() -> list[dict]:
    """云端模型列表（带 60 秒缓存）；云端挂了就回退到 config 里那个模型。"""
    now = time.time()
    if _MODELS_CACHE["data"] and now - _MODELS_CACHE["at"] < _MODELS_TTL:
        return _MODELS_CACHE["data"]
    try:
        data = await cloud.list_models()
        _MODELS_CACHE.update(at=now, data=data)
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("拉云端模型列表失败：%s", exc)
        if _MODELS_CACHE["data"]:
            return _MODELS_CACHE["data"]
        return [{"id": CFG["cloud"]["model"], "object": "model", "owned_by": "cloud"}]


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"message": message, "type": "shubing_router_error"}})


def _sse_error(message: str, model: str = "") -> str:
    return "data: " + json.dumps(
        {
            "error": {"message": message, "type": "shubing_router_error"},
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        ensure_ascii=False,
    ) + "\n\ndata: [DONE]\n\n"


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


# ------------------------------------------------------------------ 请求透传

def _forward_params(body: dict) -> dict:
    """请求侧纯透传：除了 messages/model/stream 由我们接管，其余参数原样转给云端。

    forward_params 配成数组时只透传数组里的（有些云端对多余字段会报 400 时用）。
    """
    allowed = CFG["cloud"].get("forward_params")
    skip = {"messages", "model", "stream"}
    if isinstance(allowed, list):
        forwarded = {k: body[k] for k in allowed if k in body and body[k] is not None}
    else:
        forwarded = {k: v for k, v in body.items() if k not in skip}
    forwarded.pop("stream_options", None)   # 流式由我们自己控制，不需要云端回 usage
    # 唯一的例外：ST 的 max_tokens 通常按"非思考模型"给（几百），对带原生思维链的
    # 云端模型来说会让正文被截断。这里抬到配置的下限（设 0 可关闭）。
    floor = int(CFG["cloud"].get("min_max_tokens") or 0)
    if floor and isinstance(forwarded.get("max_tokens"), int):
        forwarded["max_tokens"] = max(forwarded["max_tokens"], floor)
    return forwarded


# ------------------------------------------------------------------ 酒馆接口

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _error("请求体不是合法 JSON")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _error("messages 不能为空")

    requested_model = body.get("model")
    model = (requested_model.strip() if isinstance(requested_model, str) and requested_model.strip()
             else CFG["cloud"]["model"])
    total_chars = sum(len(m.get("content") or "") if isinstance(m.get("content"), str) else 0
                      for m in messages)
    logger.info("收到请求：%d 条消息 / %d 字 | stream=%s | 用户 model=%s",
                len(messages), total_chars, body.get("stream"), body.get("model"))

    if not cloud_key_ok():
        message = f"还没配置云端模型 key。打开 {CONFIG_PATH}，把 cloud.api_key 填上再重启。"
        if body.get("stream"):
            async def _gen():
                yield _sse_error(message, model)
            return StreamingResponse(_gen(), media_type="text/event-stream")
        return _error(message, 500)

    forwarded = _forward_params(body)
    logger.info("→ 云端纯透传：%d 条消息 / %d 字 | model=%s | 参数 %s",
                len(messages), total_chars, model, sorted(forwarded))

    if not body.get("stream", False):
        content_parts: list[str] = []
        reasoning_parts: list[str] = []

        async def emit(kind: str, text: str) -> None:
            (content_parts if kind == "content" else reasoning_parts).append(text)

        try:
            await pipeline.run(messages, forwarded, emit, model=model)
        except Exception as exc:  # noqa: BLE001
            logger.exception("流水线失败")
            return _error(str(exc), 500)
        message = {"role": "assistant", "content": "".join(content_parts)}
        reasoning = "".join(reasoning_parts)
        if reasoning:
            message["reasoning_content"] = reasoning
        return JSONResponse({
            "id": f"chatcmpl-{uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        })

    return StreamingResponse(
        stream_completion(messages, forwarded, model=model),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


@app.get("/v1/models")
async def models():
    """透传云端模型的可用模型列表（酒馆的模型下拉就是它）。"""
    return {"object": "list", "data": await _cloud_models()}


# ------------------------------------------------------------------ 测试前端

async def _sse_run(run_factory: Callable[[Callable, Callable], Awaitable[dict]]) -> StreamingResponse:
    """把 pipeline 的事件（reasoning/content/marked/done/error）包成 SSE。"""
    async def gen():
        queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        done = asyncio.Event()

        async def emit(kind: str, text: str) -> None:
            await queue.put((kind, text))

        async def on_marked(text: str) -> None:
            await queue.put(("marked", text))

        async def runner() -> None:
            try:
                stats = await run_factory(emit, on_marked)
                await queue.put(("done", stats))
            except Exception as exc:  # noqa: BLE001
                logger.exception("测试前端请求失败")
                await queue.put(("error", str(exc)))
            finally:
                done.set()

        task = asyncio.create_task(runner())
        try:
            while True:
                try:
                    kind, value = await asyncio.wait_for(queue.get(), timeout=5)
                except asyncio.TimeoutError:
                    if done.is_set() and queue.empty():
                        break
                    yield ": keepalive\n\n"
                    continue
                if kind == "done":
                    yield _sse({"kind": "done", "stats": value})
                elif kind == "error":
                    yield _sse({"kind": "error", "message": str(value)})
                elif kind == "stage":
                    name, status = str(value).split(":", 1)
                    yield _sse({"kind": "stage", "name": name, "status": status})
                else:                         # reasoning / content / marked / cloud_input / cloud_output
                    yield _sse({"kind": kind, "text": value})
        finally:
            task.cancel()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(_index_html())


@app.get("/api/samples")
async def samples():
    path = WEB_DIR / "samples.json"
    try:
        return JSONResponse(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return JSONResponse([])


@app.get("/api/info")
async def api_info():
    rewrite_ok, rewrite_msg = await rewrite.health()
    jev_ok, jev_msg = await jev.health()
    return {
        "service": "薯饼路由器",
        "port": CFG["port"],
        "st_endpoint": f"http://127.0.0.1:{CFG['port']}/v1",
        "cloud": {"configured": cloud_key_ok(), "model": CFG["cloud"]["model"],
                  "models": await _cloud_models()},
        "rewrite": {"ok": rewrite_ok, "detail": rewrite_msg,
                    "base_url": CFG["rewrite"]["base_url"], "model": CFG["rewrite"]["model"]},
        "jev": {"ok": jev_ok, "detail": jev_msg, "base_url": CFG["jev"]["base_url"]},
        "protection": CFG.get("protection") or {},
    }


@app.post("/api/rewrite")
async def api_rewrite(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _error("请求体不是合法 JSON")
    text = (body.get("text") or "").strip()
    if not text:
        return _error("贴一段要改的文本")
    return await _sse_run(lambda emit, on_marked: pipeline.rewrite_text(text, emit, on_marked))


@app.post("/api/generate")
async def api_generate(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _error("请求体不是合法 JSON")
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return _error("写一句想让云端模型写的内容")
    if not cloud_key_ok():
        return _error(f"云端模型没配置 key，先编辑 {CONFIG_PATH}", 500)
    requested_model = body.get("model")
    model = (requested_model.strip() if isinstance(requested_model, str) and requested_model.strip()
             else CFG["cloud"]["model"])
    messages: list[dict] = []
    system = (body.get("system") or "").strip()
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    forwarded: dict = {}
    if body.get("max_tokens"):
        forwarded["max_tokens"] = max(int(body["max_tokens"]),
                                      int(CFG["cloud"].get("min_max_tokens") or 0))
    return await _sse_run(
        lambda emit, on_marked: pipeline.run(messages, forwarded, emit, on_marked, model=model))


# ------------------------------------------------------------------ 状态 / 启动

@app.get("/health")
async def health():
    cloud_ok, cloud_msg = await cloud.health()
    rewrite_ok, rewrite_msg = await rewrite.health()
    jev_ok, jev_msg = await jev.health()
    return {
        "status": "ok" if (cloud_ok and rewrite_ok and jev_ok) else "degraded",
        "cloud": {"ok": cloud_ok, "detail": cloud_msg, "model": CFG["cloud"]["model"]},
        "rewrite": {"ok": rewrite_ok, "detail": rewrite_msg,
                    "base_url": CFG["rewrite"]["base_url"], "model": CFG["rewrite"]["model"]},
        "jev": {"ok": jev_ok, "detail": jev_msg, "base_url": CFG["jev"]["base_url"]},
        "config": str(CONFIG_PATH),
    }


def _lan_ips() -> list[str]:
    import socket

    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            ips.add(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


async def _print_banner() -> None:
    border = "=" * 62
    print("\n" + border)
    print("  薯饼路由器 —— 云端生成 + rewrite3 改文 一条龙")
    print(border)
    print(f"  配置文件        : {CONFIG_PATH}")
    print(f"  云端模型        : {CFG['cloud']['model']} @ {CFG['cloud']['base_url']}")
    print(f"  云端 key        : {'✓ 已配置' if cloud_key_ok() else '✗ 未配置！填 cloud.api_key'}")
    print(f"  改文模型        : {CFG['rewrite']['model']} @ {CFG['rewrite']['base_url']}")
    print(f"  jevnovel 打分   : {CFG['jev']['base_url']}")
    print(f"  监听            : {CFG['host']}:{CFG['port']}")
    print()
    print("  正在检查下游服务……")
    cloud_ok, cloud_msg = await cloud.health()
    rewrite_ok, rewrite_msg = await rewrite.health()
    jev_ok, jev_msg = await jev.health()
    for name, ok, msg in (("云端模型", cloud_ok, cloud_msg),
                          ("改文模型", rewrite_ok, rewrite_msg),
                          ("jevnovel", jev_ok, jev_msg)):
        print(f"    {'✓' if ok else '✗'} {name:<8} {msg}")
    print()
    print("  浏览器体验（不用酒馆）：")
    print(f"    http://127.0.0.1:{CFG['port']}/")
    for ip in _lan_ips():
        print(f"    局域网/其他设备  : http://{ip}:{CFG['port']}/")
    print()
    print("  SillyTavern 连接：")
    print(f"    Custom Endpoint : http://127.0.0.1:{CFG['port']}/v1")
    for ip in _lan_ips():
        print(f"    局域网/其他设备  : http://{ip}:{CFG['port']}/v1")
    print()
    if not rewrite_ok or not jev_ok:
        print("  ⚠ 改文模型/jevnovel 没连上：酒馆里会自动降级成「只输出云端原文」，")
        print("    测试前端里「直接改文」会报错。先把 tools/ 里两个服务起起来（见 README）。")
    if not cloud_ok:
        print("  ⚠ 云端模型没连上：测试前端的「云端生成」用不了，但「直接改文」不受影响。")
    print(border + "\n")


if __name__ == "__main__":
    asyncio.run(_print_banner())
    uvicorn.run(app, host=CFG["host"], port=CFG["port"], access_log=False)
