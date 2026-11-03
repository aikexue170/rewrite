"""jevnovel 打分客户端。

调 jevnove 服务的 POST /v1/score_units（入：已经切好的单元；出：逐句概率）。
兼容两种服务返回：
  · 0.8B 双头学生（kind=dual）：p = {"可以保留": x, "需要修改": y}，bad=None
  · 27B / 多分类：p 里带 "烂完了" / "不好" / "坏"，或者直接给 bad 字段
统一折算成一个 0~1 的"烂"分数；拿不到分（跳过/超长/出错）返回 None。
"""
import asyncio
import logging

import httpx

from .config import CFG

logger = logging.getLogger(__name__)

BAD_KEYS = ("需要修改", "烂完了", "不好", "坏")


class JevError(RuntimeError):
    pass


def _extract_score(item: dict) -> float | None:
    if not isinstance(item, dict) or item.get("error"):
        return None
    if item.get("pick") == "跳过":
        return None
    value = item.get("bad")
    if isinstance(value, (int, float)):
        return float(value)
    probs = item.get("p") or {}
    for key in BAD_KEYS:
        value = probs.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    heat = item.get("heat")
    if isinstance(heat, (int, float)):
        return float(heat)
    return None


def _split_long(units: list[str], chunk_chars: int) -> tuple[list[str], list[list[int]]]:
    """把超过 chunk_chars 的单元切成小块；返回 (送分块列表, 每个单元对应的块下标)。"""
    pieces: list[str] = []
    unit_pieces: list[list[int]] = []
    for unit in units:
        if chunk_chars <= 0 or len(unit) <= chunk_chars:
            unit_pieces.append([len(pieces)])
            pieces.append(unit)
            continue
        idxs = []
        for i in range(0, len(unit), chunk_chars):
            idxs.append(len(pieces))
            pieces.append(unit[i:i + chunk_chars])
        unit_pieces.append(idxs)
    return pieces, unit_pieces


async def score_units(units: list[str]) -> list[float | None]:
    """给一批单元打分；失败直接抛异常，由上层决定降级。"""
    if not units:
        return []
    cfg = CFG["jev"]
    chunk_chars = int(cfg.get("chunk_chars") or 300)
    batch_units = max(1, int(cfg.get("batch_units") or 120))
    pieces, unit_pieces = _split_long(units, chunk_chars)

    url = cfg["base_url"].rstrip("/") + "/v1/score_units"
    scores = [None] * len(pieces)
    timeout = httpx.Timeout(float(cfg.get("timeout_s") or 120), connect=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for start in range(0, len(pieces), batch_units):
            batch = pieces[start:start + batch_units]
            try:
                resp = await client.post(url, json={"units": batch})
            except httpx.HTTPError as exc:
                raise JevError(f"连不上 jevnovel 服务（{url}）：{exc}") from exc
            if resp.status_code >= 400:
                raise JevError(f"jevnovel 返回 {resp.status_code}：{resp.text[:300]}")
            data = resp.json()
            items = data.get("items") or data.get("sentences") or []
            if len(items) != len(batch):
                raise JevError(f"jevnovel 返回条数不对：送 {len(batch)} 句，回 {len(items)} 句")
            for offset, item in enumerate(items):
                scores[start + offset] = _extract_score(item)

    result: list[float | None] = []
    for idxs in unit_pieces:
        vals = [scores[i] for i in idxs if scores[i] is not None]
        result.append(max(vals) if vals else None)   # 长单元按块取最烂
    return result


async def health(timeout: float = 3.0) -> tuple[bool, str]:
    """探测 jev 服务是否活着（启动横幅用）。"""
    url = CFG["jev"]["base_url"].rstrip("/") + "/health"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
        if resp.status_code < 400:
            return True, f"{resp.status_code}"
        return False, f"HTTP {resp.status_code}"
    except httpx.HTTPError as exc:
        return False, str(exc)[:80]


async def score_texts_for_debug(texts: list[str]) -> list[float | None]:
    return await score_units(texts)
