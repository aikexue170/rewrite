"""主流水线：云端生成 → 分句打标 → 整篇/分段改文 → 按顺序吐给前端。

用户看到的顺序：
  1. 云端模型的原生思维链（实时）
  2. 〔rewrite3思维链〕 标记
  3. 改文模型的思维链（实时）
  4. 改文后的正文（实时）

默认「思维链全部出完，正文再出来」（pipeline.body_after_cot = true）：
前端卡的内容会被 protection 切成 正文段 + 结构保留段，如果边改边吐，前一段正文会在
后一段思维链之前冒出来，看起来就是"思维链还没出完，正文先蹦出来了"。所以默认先把
所有段的改文思维链流完，最后再按顺序把正文流出去（SSE 层有打字机节奏）。

云端模型的正文全程不直接展示，只在后台缓冲，等它写完再送进改文模型。
前端卡的结构内容（HTML/代码块/状态栏/变量更新块/占位符/宏）先被 protection 切成
「原样保留」段，只有纯正文段会被送去改文。

除了服务酒馆，还提供 rewrite_text()：直接给一段文本（测试前端用），不走云端。
"""
import json
import logging
import time
from typing import Awaitable, Callable

from . import cloud, jev, rewrite, splitter
from .config import CFG
from .protection import Part, plan_reply

logger = logging.getLogger(__name__)

Emit = Callable[[str, str], Awaitable[None]]
MarkedHook = Callable[[str], Awaitable[None]]


def _protection_cfg() -> dict:
    cfg = CFG.get("protection") or {}
    return {
        "min_prose_ratio": float(cfg.get("min_prose_ratio", 0.15)),
        "max_segments": int(cfg.get("max_segments", 4)),
        "min_rewrite_chars": int(cfg.get("min_rewrite_chars", 60)),
    }


async def _mark_text(text: str) -> tuple[str, int, float | None]:
    """给一段正文打上【分数】标记。jev 挂了就全标【NA】，不中断流程。"""
    units = splitter.split_units(text)
    if not units:
        return text + "【NA】", 0, None
    units_text = [unit for _, unit in units]
    try:
        scores = await jev.score_units(units_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("jev 打分失败，本段全部按【NA】处理：%s", exc)
        scores = [None] * len(units_text)
    marked = splitter.build_marked(text, scores)
    values = [s for s in scores if s is not None]
    mean = round(sum(values) / len(values), 4) if values else None
    return marked, len(units), mean


def _on_reasoning(emit: Emit) -> Callable[[str], Awaitable[None]]:
    async def _callback(text: str) -> None:
        await emit("reasoning", text)
    return _callback


async def _process_body(body: str, had_cot: bool, emit: Emit,
                        on_marked: MarkedHook | None = None) -> dict:
    """把一段「云端输出」处理后流式发出去（改文 / 跳过 / 降级都在这里）。"""
    pcfg = CFG["pipeline"]
    header = (pcfg.get("rewrite_cot_header") or "").strip()
    fallback = bool(pcfg.get("fallback_to_original", True))
    max_chars = int(pcfg.get("max_source_chars") or 0)
    protection_on = bool((CFG.get("protection") or {}).get("enabled", True))
    note = (CFG.get("protection") or {}).get("note_in_reasoning", True)
    min_chars = int(_protection_cfg()["min_rewrite_chars"])
    # true = 思维链全部出完再吐正文（默认）；false = 每段改完就吐
    defer_body = bool(pcfg.get("body_after_cot", True))

    stats = {"chars_in": len(body), "chars_out": 0, "seconds": 0.0,
             "skipped": False, "reason": "", "rewritten": 0}
    started = time.time()
    header_sent = False
    out_chunks: list[str] = []          # 缓冲模式的正文片段（含结构保留段）
    out_chars = 0
    pending_keep: list[str] = []        # 立即模式下，正文开始前先压住的结构段
    content_started = False
    emitted_content = False

    async def flush_pending_keep() -> None:
        nonlocal out_chars, content_started, emitted_content
        while pending_keep:
            text = pending_keep.pop(0)
            await emit("content", text)
            emitted_content = True
            out_chars += len(text)
        content_started = True

    async def add_content(text: str, is_keep: bool = False) -> None:
        """收集/发送正文片段。缓冲模式先存着，最后统一吐。"""
        nonlocal out_chars, content_started
        if not text:
            return
        if defer_body:
            out_chunks.append(text)
            out_chars += len(text)
            return
        if is_keep and not content_started:
            pending_keep.append(text)
            return
        if not content_started:
            await flush_pending_keep()
        out_chars += len(text)
        await emit("content", text)

    plan = plan_reply(body, **_protection_cfg()) if protection_on else None

    if plan is not None and plan.skipped:
        logger.info("检测到前端结构，整条跳过改文：%s", plan.reason)
        if note:
            await emit("reasoning", f"\n\n〔本条检测到前端结构，跳过改文：{plan.reason}〕\n")
        await emit("stage", "rewrite:skip")
        await emit("content", body)
        stats.update(skipped=True, reason=plan.reason, chars_out=len(body),
                     seconds=round(time.time() - started, 2))
        return stats

    parts: list[Part] = plan.parts if plan is not None else [Part("prose", body)]
    logger.info("正文 %d 字符（原生思维链：%s），可改正文 %d 字符，切分：%s",
                len(body), "有" if had_cot else "无",
                sum(len(p.text.strip()) for p in parts if p.kind == "prose"),
                "+".join("正文" if p.kind == "prose" else "保留" for p in parts))

    for part in parts:
        if part.kind == "keep":
            await add_content(part.text, is_keep=True)
            continue

        text = part.text
        lead = text[:len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        core = text.strip()
        if len(core) < min_chars:
            await add_content(text)
            continue

        try:
            if not header_sent:
                if header:
                    await emit("reasoning", ("\n\n" if had_cot else "") + header + "\n\n")
                header_sent = True
            elif header:
                await emit("reasoning", "\n\n")

            chunks = splitter.split_body_chunks(core, max_chars)
            await add_content(lead)
            for ci, chunk in enumerate(chunks):
                if ci > 0:
                    await add_content("\n\n")
                await emit("stage", "jev:start")
                try:
                    marked, n_units, mean = await _mark_text(chunk)
                finally:
                    await emit("stage", "jev:done")
                if on_marked is not None:
                    await on_marked(marked)
                t0 = time.time()

                cur_body: list[str] = []

                async def on_content(piece: str) -> None:
                    nonlocal emitted_content, content_started
                    if defer_body:
                        cur_body.append(piece)
                        return
                    if not content_started:
                        await flush_pending_keep()
                    emitted_content = True
                    await emit("content", piece)

                await emit("stage", "rewrite:start")
                try:
                    rewritten = await rewrite.stream_rewrite(
                        marked, _on_reasoning(emit), on_content)
                finally:
                    await emit("stage", "rewrite:done")
                if defer_body:
                    body_piece = "".join(cur_body)
                    if not body_piece and not rewritten:
                        raise rewrite.RewriteError("改文模型没有返回正文")
                    await add_content(body_piece or rewritten)
                elif not rewritten:
                    raise rewrite.RewriteError("改文模型没有返回正文")
                stats["rewritten"] += 1
                logger.info("改写完成 | 原 %d 字 → 改写 %d 字 | units=%d mean=%s | %.1fs",
                            len(chunk), len(rewritten), n_units, mean, time.time() - t0)
            await add_content(trail)
        except Exception as exc:  # noqa: BLE001
            logger.warning("改文失败：%s", exc)
            if not fallback:
                raise
            if defer_body:
                await add_content(core)
                await add_content(trail)
                break
            if not emitted_content and not out_chunks:
                logger.warning("降级：直接输出原文")
                await emit("content", body)
                stats["chars_out"] = len(body)
                stats["seconds"] = round(time.time() - started, 2)
                return stats
            await add_content(core)
            await add_content(trail)
            break

    # 缓冲模式：所有思维链出完了，现在按顺序把正文流出去
    if defer_body:
        for piece in out_chunks:
            await emit("content", piece)
    elif pending_keep:
        await flush_pending_keep()

    stats["chars_out"] = out_chars
    stats["seconds"] = round(time.time() - started, 2)
    _dump_debug(body, parts, stats)
    return stats


def _dump_debug(body: str, parts, stats: dict) -> None:
    """把最近一次改文的原文和切分结果写到 debug/ 下，方便排查（config.debug.dump）。"""
    try:
        dbg = CFG.get("debug") or {}
        if not dbg.get("dump"):
            return
        from .config import BASE_DIR
        out_dir = BASE_DIR / "debug"
        out_dir.mkdir(exist_ok=True)
        lines = [f"chars_in={stats['chars_in']} chars_out={stats['chars_out']} "
                 f"rewritten={stats['rewritten']} skipped={stats['skipped']}"]
        for i, part in enumerate(parts):
            preview = part.text.strip().replace("\n", "⏎")[:120]
            lines.append(f"[{i}] {part.kind:5s} len={len(part.text):4d} | {preview}")
        (out_dir / "last_plan.txt").write_text("\n".join(lines) + "\n\n===== 原文 =====\n" + body,
                                               encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _dump_messages(messages: list[dict], model: str | None) -> None:
    """把最近一次实际发给云端的 messages 写下来（排查提示词用）。"""
    try:
        dbg = CFG.get("debug") or {}
        if not dbg.get("dump"):
            return
        from .config import BASE_DIR
        out_dir = BASE_DIR / "debug"
        out_dir.mkdir(exist_ok=True)
        parts = [f"model={model or CFG['cloud']['model']} | messages={len(messages)}"]
        for i, m in enumerate(messages):
            content = m.get("content")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            parts.append(f"\n{'='*70}\n[{i}] role={m.get('role')} | {len(content)} 字\n{content}")
        (out_dir / "last_messages.txt").write_text("\n".join(parts), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


async def run(messages: list[dict], forwarded_params: dict, emit: Emit,
              on_marked: MarkedHook | None = None, model: str | None = None) -> dict:
    """完整链路：调云端模型，拿回正文后再改文。model 为空时用 config 里的默认模型。"""
    _dump_messages(messages, model)
    await emit("cloud_input", json.dumps(messages, ensure_ascii=False))
    await emit("stage", "cloud:start")
    try:
        body, had_cot = await cloud.stream_cloud(
            messages, forwarded_params, _on_reasoning(emit), model=model)
    except Exception:
        await emit("stage", "cloud:error")
        raise
    await emit("stage", "cloud:done")
    body = body.strip()
    if not body:
        raise RuntimeError("云端模型没有返回正文")
    await emit("cloud_output", body)
    return await _process_body(body, had_cot, emit, on_marked)


async def rewrite_text(text: str, emit: Emit,
                       on_marked: MarkedHook | None = None) -> dict:
    """直接改一段文本（测试前端用，不经过云端模型）。"""
    body = (text or "").strip()
    if not body:
        raise ValueError("贴一段要改的文本")
    return await _process_body(body, False, emit, on_marked)
