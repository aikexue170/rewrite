"""SSE 输出：把流水线事件包装成 OpenAI 兼容的 chat.completion.chunk 流。

- reasoning 事件 → delta.reasoning_content（ST 显示为灰色思考块）
- content 事件 → delta.content（正文）
- 长时间没有事件（比如云端正在写正文、改文模型在憋思维链）→ 发空心跳保活
"""
import asyncio
import json
import logging
import time
import uuid
from typing import AsyncGenerator

from . import pipeline
from .config import CFG

logger = logging.getLogger(__name__)



def _sse_line(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _base_chunk(created: int, model: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
    }


def _reasoning_chunk(created: int, model: str, text: str) -> str:
    obj = _base_chunk(created, model)
    obj["choices"] = [{"index": 0, "delta": {"reasoning_content": text}, "finish_reason": None}]
    return _sse_line(obj)


def _content_chunk(created: int, model: str, text: str) -> str:
    obj = _base_chunk(created, model)
    obj["choices"] = [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
    return _sse_line(obj)


def _heartbeat_chunk(created: int, model: str) -> str:
    obj = _base_chunk(created, model)
    obj["choices"] = [{"index": 0, "delta": {"content": ""}, "finish_reason": None}]
    return _sse_line(obj)


def _finish_chunk(created: int, model: str) -> str:
    obj = _base_chunk(created, model)
    obj["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    return _sse_line(obj)


def _error_chunk(message: str, created: int, model: str) -> str:
    obj = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "error": {"message": message, "type": "rewrite_router_error"},
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return _sse_line(obj)


async def stream_completion(messages: list[dict], forwarded_params: dict,
                            model: str | None = None) -> AsyncGenerator[str, None]:
    cfg = CFG["sse"]
    cloud_model = model                      # 真正发给云端的模型名
    model = model or CFG["cloud"]["model"]   # SSE 里展示的模型名
    created = int(time.time())
    heartbeat = float(cfg.get("heartbeat_interval_s") or 5)
    chunk_size = max(1, int(cfg.get("chunk_size") or 16))
    chunk_interval = float(cfg.get("chunk_interval_s") or 0)

    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
    done = asyncio.Event()
    errors: dict = {}

    async def emit(kind: str, text: str) -> None:
        await queue.put((kind, text))

    async def runner() -> None:
        try:
            await pipeline.run(messages, forwarded_params, emit, model=cloud_model)
        except Exception as exc:  # noqa: BLE001
            errors["error"] = exc
            logger.exception("流水线异常")
        finally:
            done.set()

    task = asyncio.create_task(runner())
    try:
        while True:
            try:
                kind, text = await asyncio.wait_for(queue.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                if done.is_set() and queue.empty():
                    break
                yield _heartbeat_chunk(created, model)
                continue

            if kind == "reasoning":
                yield _reasoning_chunk(created, model, text)
            elif kind != "content":
                continue                      # stage/marked/cloud_input 之类只给测试前端用
            else:
                for i in range(0, len(text), chunk_size):
                    yield _content_chunk(created, model, text[i:i + chunk_size])
                    if chunk_interval > 0:
                        await asyncio.sleep(chunk_interval)

        if "error" in errors:
            yield _error_chunk(str(errors["error"]), created, model)
        yield _finish_chunk(created, model)
        yield "data: [DONE]\n\n"
    finally:
        task.cancel()
