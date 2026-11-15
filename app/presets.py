"""预设解析：识别用户当前在用的 ST 预设，按预设规则抽出「可改写正文」。

背景（见 施工记录 11-13.md 20:30 段）：不同 SillyTavern 预设的模型回复不是同一种
结构。梦鲸思客 V5 输出 ``<dream_think>``（规划）/ ``<dream_scene>``（场景栏）/
``<dream_body>``（真正文）/ ``<dream_after_format>``（状态栏），预设自带的正则还会在
显示/历史里剥掉一部分标签；另外一些预设的正文压根不在标签里，只能靠锚点定位。

通用保护层（``protection.plan_reply``）是按「标签名像不像正文」猜的，遇到这类预设会
把规划当正文改写，或者把整条判成前端结构直接跳过。本模块把「这个预设的正文在哪」
变成可配置的数据：

* spec 文件：站点根 ``preset_specs/*.json``（``presets.dir`` 可改）。加新预设 =
  往文件夹里加一个 spec（用 ``tools/parse_preset.py`` 从 ST 预设 JSON 生成草稿，
  人工补齐结构规则后可上线），不需要改代码。
* 匹配：两条通道——入站 messages 里预设提示词做长句指纹比对（ST 托管路径最准），
  或正文里的结构标记（``markers``）比对；都匹配不上就返回 None，调用方回退到
  原有 ``protection.plan_reply``。
* 抽取：按 spec 的 ``keep_tags`` / ``body_tags`` / ``plain_body`` 锚点切分，
  未闭合的 keep 标签保护到结尾（绝不把规划当正文）；正文区内部再交给通用保护层
  做二次切分（HTML 面板、变量块等仍然原样保留）。

spec JSON 结构见 ``preset_specs/README.md``。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import config, protection

logger = logging.getLogger(__name__)

_SPEC_SUFFIX = ".json"
_DEFAULT_DIR = "preset_specs"
_DEFAULT_MIN_FINGERPRINT_CHARS = 20
_DEFAULT_MIN_MARKERS = 1
_DEFAULT_MIN_TEXT_SCORE = 8

# 标签 token（比 protection 的宽松：预设标签常见下划线/冒号，允许属性）
_TAG_RE = re.compile(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9_.:\-~]*)\b([^<>]*?)(/?)>", re.S)
# 中文标签（图灵/夏瑾有 <平行世界>、<正文> 这类）
_CJK_TAG_RE = re.compile(r"<\s*(/?)\s*([\u4e00-\u9fff]{1,6})\s*(/?)>")


@dataclass
class PresetSpec:
    """一份预设的结构解析结果（从 spec 文件读出，不含运行时状态）。"""

    id: str
    name: str
    source_file: str = ""
    # 匹配
    fingerprints: list[str] = field(default_factory=list)
    fingerprint_weights: list[float] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    display_markers: list[str] = field(default_factory=list)
    min_fingerprints: int = 1
    min_markers: int = _DEFAULT_MIN_MARKERS
    min_text_score: int = _DEFAULT_MIN_TEXT_SCORE
    # 结构
    keep_tags: list[str] = field(default_factory=list)
    body_tags: list[str] = field(default_factory=list)
    wrapper_tags: list[str] = field(default_factory=list)
    # 根/外层标签：只保留标签 token 本身，内部内容继续按其他规则处理（如 <dream_plot>）
    root_tags: list[str] = field(default_factory=list)
    plain_body_after: list[str] = field(default_factory=list)
    plain_body_before: list[str] = field(default_factory=list)
    keep_prefixes: list[str] = field(default_factory=list)
    body_is_whole: bool = False
    # plain_body.after 一个都没命中时的起点（None = 不启用；夏瑾 display 态用 0）
    plain_start_if_no_after: int | None = None
    # body 标签/锚点都找不到正文时，退而取「第一个 keep 块之后的最大空白区」当正文
    # （有的 Izumi 变体把正文写在 </tucao> 之后、且不带 <content> 标签）
    plain_fallback_after_keep: bool = False
    # 资料（仅展示/审计，不参与抽取）
    regex_scripts: list[dict] = field(default_factory=list)
    tavern_helper_scripts: list[dict] = field(default_factory=list)
    notes: str = ""

    def keep_tag_set(self) -> set[str]:
        return {t.lower() for t in self.keep_tags}

    def body_tag_set(self) -> set[str]:
        return {t.lower() for t in self.body_tags}

    def wrapper_tag_set(self) -> set[str]:
        return {t.lower() for t in self.wrapper_tags}

    def root_tag_set(self) -> set[str]:
        return {t.lower() for t in self.root_tags}


@dataclass
class Match:
    """一次匹配结果。"""

    spec: PresetSpec
    score: float
    channel: str            # "messages" | "text"
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------- spec 加载


def spec_dir() -> Path:
    root = Path(config.ROOT)
    raw = config.get("presets.dir", _DEFAULT_DIR)
    path = Path(str(raw)) if raw else Path(_DEFAULT_DIR)
    if not path.is_absolute():
        path = root / path
    return path


def _spec_from_json(payload: dict, *, where: str) -> PresetSpec | None:
    """把一份 spec JSON 转成 ``PresetSpec``；字段缺失/类型不对的项尽量容错。"""
    if not isinstance(payload, dict):
        logger.warning("预设 spec 不是对象，跳过：%s", where)
        return None
    spec_id = str(payload.get("id") or "").strip()
    if not spec_id:
        logger.warning("预设 spec 缺 id，跳过：%s", where)
        return None
    match = payload.get("match") or {}
    if not isinstance(match, dict):
        match = {}
    structure = payload.get("structure") or {}
    if not isinstance(structure, dict):
        structure = {}
    plain = structure.get("plain_body") or {}
    if not isinstance(plain, dict):
        plain = {}

    def _str_list(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        out: list[str] = []
        for item in value:
            text = str(item).strip() if item is not None else ""
            if text and text not in out:
                out.append(text)
        return out

    fingerprints: list[str] = []
    weights: list[float] = []
    for item in match.get("fingerprints") or []:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            weight = float(item.get("weight", 1.0) if item.get("default_enabled") is not False
                           else item.get("weight", 0.5))
        else:
            text, weight = str(item or "").strip(), 1.0
        if text and text not in fingerprints:
            fingerprints.append(text)
            weights.append(weight)
    if not weights:
        weights = [1.0] * len(fingerprints)

    return PresetSpec(
        id=spec_id,
        name=str(payload.get("name") or spec_id),
        source_file=str(payload.get("source_file") or ""),
        fingerprints=fingerprints,
        fingerprint_weights=weights,
        markers=_str_list(match.get("markers")),
        display_markers=_str_list(match.get("display_markers")),
        min_fingerprints=_as_int(match.get("min_fingerprints"), 1),
        min_markers=_as_int(match.get("min_markers"), _DEFAULT_MIN_MARKERS),
        min_text_score=_as_int(match.get("min_text_score"), _DEFAULT_MIN_TEXT_SCORE),
        keep_tags=_str_list(structure.get("keep_tags")),
        body_tags=_str_list(structure.get("body_tags")),
        wrapper_tags=_str_list(structure.get("wrapper_tags")),
        root_tags=_str_list(structure.get("root_tags")),
        plain_body_after=_str_list(plain.get("after")),
        plain_body_before=_str_list(plain.get("before")),
        keep_prefixes=_str_list(structure.get("keep_prefixes")),
        body_is_whole=bool(structure.get("body_is_whole")),
        plain_start_if_no_after=_as_optional_int(plain.get("start_if_no_after")),
        plain_fallback_after_keep=bool(plain.get("fallback_after_keep")),
        regex_scripts=[s for s in (payload.get("regex_scripts") or [])
                       if isinstance(s, dict)],
        tavern_helper_scripts=[s for s in (payload.get("tavern_helper_scripts") or [])
                               if isinstance(s, dict)],
        notes=str(payload.get("notes") or ""),
    )


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# (目录 mtime 签名 → spec 表) 缓存：改/加 spec 文件后下一个请求自动生效，无需重启。
_CACHE: dict[str, tuple[tuple, dict[str, PresetSpec]]] = {}
_CACHE_LOCK = threading.Lock()


def _dir_signature(path: Path) -> tuple:
    try:
        entries = sorted(
            (p.name, p.stat().st_mtime_ns, p.stat().st_size)
            for p in path.iterdir() if p.suffix == _SPEC_SUFFIX and p.is_file())
    except OSError:
        return ()
    return tuple(entries)


def load_specs(path: Path | None = None) -> dict[str, PresetSpec]:
    """读取并缓存某个目录下的全部 spec；目录内容变了就重新加载。"""
    directory = path or spec_dir()
    signature = _dir_signature(directory)
    cache_key = str(directory)
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and cached[0] == signature:
            return cached[1]
    specs: dict[str, PresetSpec] = {}
    if signature:
        for name, _mtime, _size in signature:
            file = directory / name
            try:
                payload = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning("预设 spec 读取失败（%s）：%s", file, exc)
                continue
            spec = _spec_from_json(payload, where=str(file))
            if spec is not None:
                if spec.id in specs:
                    logger.warning("预设 spec id 重复：%s（%s），后者被忽略", spec.id, file)
                    continue
                specs[spec.id] = spec
    else:
        logger.debug("没有预设 spec：%s", directory)
    with _CACHE_LOCK:
        _CACHE[cache_key] = (signature, specs)
    return specs


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------- 匹配


def _message_text(messages: Iterable[dict]) -> str:
    parts: list[str] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) and content:
            parts.append(content)
        elif isinstance(content, list):        # 多模态 content 数组（只取文本块）
            for piece in content:
                if isinstance(piece, dict) and isinstance(piece.get("text"), str):
                    parts.append(piece["text"])
    return "\n".join(parts)


def match_messages(messages: Iterable[dict], specs: dict[str, PresetSpec] | None = None,
                   *, min_chars: int | None = None) -> Match | None:
    """用入站 messages 里的预设提示词指纹匹配（ST 托管路径）。

    指纹是预设 prompt 条目里的独特长句；命中越长/越多分数越高。返回最高分，
    没命中返回 None。
    """
    table = specs if specs is not None else load_specs()
    if not table:
        return None
    text = _message_text(messages)
    if not text:
        return None
    threshold = min_chars if min_chars is not None else _DEFAULT_MIN_FINGERPRINT_CHARS
    best: Match | None = None
    for spec in table.values():
        weights = spec.fingerprint_weights or [1.0] * len(spec.fingerprints)
        hits = [fp for fp in spec.fingerprints if len(fp) >= threshold and fp in text]
        if len(hits) < max(1, spec.min_fingerprints):
            continue
        score = float(sum(len(fp) * weights[spec.fingerprints.index(fp)]
                          for fp in hits))
        if best is None or score > best.score:
            best = Match(spec=spec, score=score, channel="messages",
                         detail={"hits": len(hits), "fingerprints": hits[:3]})
    return best


def match_text(text: str, specs: dict[str, PresetSpec] | None = None) -> Match | None:
    """用正文里的结构标记匹配（网页粘贴路径 / messages 没命中时的兜底）。"""
    table = specs if specs is not None else load_specs()
    if not table or not text:
        return None
    best: Match | None = None
    for spec in table.values():
        raw_hits = [marker for marker in spec.markers if marker in text]
        display_hits = [marker for marker in spec.display_markers if marker in text]
        hits = raw_hits + display_hits
        if len(hits) < spec.min_markers:
            continue
        score = float(sum(len(marker) for marker in hits))
        if score < spec.min_text_score:
            continue
        if best is None or score > best.score:
            best = Match(spec=spec, score=score, channel="text",
                         detail={"hits": len(hits), "markers": hits[:5]})
    return best


def match(messages: Iterable[dict] | None = None, text: str = "",
          specs: dict[str, PresetSpec] | None = None) -> Match | None:
    """两条通道依次匹配：先 messages 指纹，再正文标记。"""
    found = match_messages(messages, specs) if messages else None
    if found is not None:
        return found
    return match_text(text, specs) if text else None


# ---------------------------------------------------------------- 结构抽取


def _tag_events(text: str, names: set[str]) -> list[tuple[int, int, str, bool, bool]]:
    """提取关注标签的 token：``(start, end, name, closing, selfclose)``。"""
    if not names:
        return []
    events: list[tuple[int, int, str, bool, bool]] = []
    for match in _TAG_RE.finditer(text):
        name = match.group(2).lower()
        if name in names:
            events.append((match.start(), match.end(), name,
                           bool(match.group(1)), bool(match.group(4))))
    for match in _CJK_TAG_RE.finditer(text):
        name = match.group(2)
        if name in names:
            events.append((match.start(), match.end(), name,
                           bool(match.group(1)), bool(match.group(3))))
    events.sort()
    return events


def _blocks(text: str, names: set[str], *,
            protect_unclosed: bool = True) -> list[tuple[int, int]]:
    """把 ``names`` 里的标签按配对切成整块 span（含标签本身）。

    - 自闭合/孤立闭标签：只保护标签本身；
    - 未闭合开标签：默认保护到结尾（宁可整条不改，也不把规划/状态栏当正文）；
      ``protect_unclosed=False`` 时忽略未闭合标签（只给 ``fallback_after_keep``
      的兜底路径用：某些 Izumi 变体的 ``<konatan_planning~>`` 不闭合）；
    - 同名嵌套按栈配对。
    """
    events = _tag_events(text, names)
    blocks: list[tuple[int, int]] = []
    stack: list[tuple[str, int]] = []
    for start, end, name, closing, selfclose in events:
        if selfclose:
            blocks.append((start, end))
            continue
        if not closing:
            stack.append((name, start))
            continue
        # 闭标签：从栈顶找同名开标签（中间没闭合的算未配对，各自保护到闭标签前）
        index = -1
        for i in range(len(stack) - 1, -1, -1):
            if stack[i][0] == name:
                index = i
                break
        if index < 0:
            blocks.append((start, end))        # 孤立闭标签：保护标签本身
            continue
        open_start = stack[index][1]
        for _name, stray_start in stack[index + 1:]:
            blocks.append((stray_start, max(stray_start, start)))
        del stack[index:]
        blocks.append((open_start, end))
    if protect_unclosed:
        for _name, stray_start in stack:
            blocks.append((stray_start, len(text)))
    return _merge(blocks)


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    ordered = sorted(spans)
    out = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(int(s), int(e)) for s, e in out]


def _prefix_blocks(text: str, prefixes: list[str]) -> list[tuple[int, int]]:
    """固定前缀句（如"好的，我将进行符合需求的创作："）整句保留。"""
    for prefix in prefixes:
        if not prefix:
            continue
        stripped = text.lstrip()
        if stripped.startswith(prefix):
            start = len(text) - len(stripped)
            return [(start, start + len(prefix))]
    return []


def _subtract(spans: list[tuple[int, int]],
              holes: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """从 ``spans`` 里挖掉 ``holes``。"""
    result: list[tuple[int, int]] = []
    for start, end in spans:
        cursor = start
        for cut_start, cut_end in holes:
            if cut_end <= cursor or cut_start >= end:
                continue
            if cut_start > cursor:
                result.append((cursor, min(cut_start, end)))
            cursor = max(cursor, cut_end)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return result


def _inner_spans(text: str, names: set[str]) -> list[tuple[int, int]]:
    """``names`` 标签的「内部」span（不含标签本身）；未闭合开标签算到结尾。"""
    events = _tag_events(text, names)
    spans: list[tuple[int, int]] = []
    stack: list[tuple[str, int]] = []
    for start, end, name, closing, selfclose in events:
        if selfclose:
            continue
        if not closing:
            stack.append((name, end))
            continue
        index = -1
        for i in range(len(stack) - 1, -1, -1):
            if stack[i][0] == name:
                index = i
                break
        if index < 0:
            continue
        inner_start = stack[index][1]
        del stack[index:]
        spans.append((inner_start, start))
    for _name, inner_start in stack:
        spans.append((inner_start, len(text)))
    return _merge(spans)


def _fallback_after_keep_span(text: str, keep_spans: list[tuple[int, int]],
                              min_rewrite_chars: int) -> tuple[int, int] | None:
    """body 标签/锚点都失效时的兜底：第一个 keep 块之后、最大的非 keep 空白区。

    只对显式开启 ``plain_body.fallback_after_keep`` 的 spec 生效（目前是 Izumi 的
    某个变体：正文写在 ``</tucao>`` 之后且不带 ``<content>`` 标签）。规划在第一个
    keep 块之前，所以不会被选中；候选区长度必须 >= ``min_rewrite_chars``。
    """
    merged = _merge(list(keep_spans))
    if not merged:
        return None
    first_keep_end = merged[0][1]
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < len(text):
        gaps.append((cursor, len(text)))
    candidates = [(start, end) for start, end in gaps
                  if start >= first_keep_end and (end - start) >= min_rewrite_chars]
    if not candidates:
        return None
    return max(candidates, key=lambda span: span[1] - span[0])


def _plain_body_span(text: str, spec: PresetSpec) -> tuple[int, int] | None:
    """没有 body 标签时用锚点定位正文区：最后一个 after 之后、其后的第一个 before 之前。"""
    start = -1
    for anchor in spec.plain_body_after:
        index = text.rfind(anchor)
        if index >= 0:
            start = max(start, index + len(anchor))
    if start < 0:
        if spec.plain_start_if_no_after is None:
            return None
        start = max(0, min(spec.plain_start_if_no_after, len(text)))
    end = len(text)
    for anchor in spec.plain_body_before:
        index = text.find(anchor, start)
        if index >= 0:
            end = min(end, index)
    if end <= start:
        return None
    return start, end


def _prose_parts(region: str, protection_cfg: dict | None,
                 min_rewrite_chars: int) -> list[protection.Part]:
    """正文区内部再过一遍通用保护层（HTML 面板/变量块仍然原样保留）。"""
    if not region.strip():
        return [protection.Part("keep", region)]
    cfg = dict(protection_cfg or {})
    cfg.pop("min_rewrite_chars", None)
    try:
        plan = protection.plan_reply(region, **cfg)
    except TypeError:                      # 配置字段和函数签名不匹配时退回最保守调用
        plan = protection.plan_reply(region)
    if plan.skipped or not plan.parts:
        # 预设已明确这是正文区，通用保护层判「结构太多」时不能整条放弃：
        # 仍然把整区当正文，让改写发生（逻辑能力优先），长度不足的后面会被跳过。
        if len(region.strip()) >= min_rewrite_chars:
            return [protection.Part("prose", region)]
        return [protection.Part("keep", region)]
    return plan.parts


def plan_reply(text: str, spec: PresetSpec, *,
               protection_cfg: dict | None = None,
               min_rewrite_chars: int = 60) -> protection.Plan | None:
    """按预设 spec 切分模型回复；找不到正文区时返回 None（调用方回退通用保护层）。

    返回的 ``Plan.parts`` 覆盖原文全部分字符：``keep`` 原样输出，``prose`` 送改文。
    """
    if not text or not text.strip():
        return None
    keep_names = spec.keep_tag_set()
    body_names = spec.body_tag_set() | spec.wrapper_tag_set()
    root_names = spec.root_tag_set()
    # keep_tags：整块保护（含标签）；root_tags：只保留标签 token，内容继续按规则处理
    keep_spans = _blocks(text, keep_names) if keep_names else []
    root_spans: list[tuple[int, int]] = []
    if root_names:
        root_spans = [(start, end) for start, end, _name, _closing,
                      _selfclose in _tag_events(text, root_names)]
        keep_spans = _merge(keep_spans + root_spans)
    prefix_spans = _prefix_blocks(text, spec.keep_prefixes)
    keep_spans = _merge(keep_spans + prefix_spans)
    has_structure = bool(_tag_events(text, keep_names | body_names | root_names))

    body_spans: list[tuple[int, int]] = []
    if body_names:
        body_spans = _subtract(_inner_spans(text, body_names), keep_spans)
    if not body_spans and (spec.plain_body_after or spec.plain_body_before):
        plain = _plain_body_span(text, spec)
        if plain is not None:
            body_spans = _subtract([plain], keep_spans)
            # 锚点命中但正文全被 keep 块吃掉时，仍算「结构存在」→ 走透传
            if body_spans:
                has_structure = True
    if not body_spans and spec.body_is_whole:
        has_structure = True
        body_spans = _subtract([(0, len(text))], keep_spans)
    if spec.plain_fallback_after_keep:
        # 锚点只切出空白/换行（正文其实在 </tucao> 之后）时，当成没找到正文；
        # 兜底时忽略未闭合 keep 标签（否则不闭合的 <konatan_planning~> 会吞掉全文）。
        found_chars = sum(len(text[start:end].strip()) for start, end in body_spans)
        if found_chars < min_rewrite_chars:
            fallback_keep = _blocks(text, keep_names, protect_unclosed=False) if keep_names else []
            fallback_keep = _merge(fallback_keep + root_spans + prefix_spans)
            fallback = _fallback_after_keep_span(text, fallback_keep, min_rewrite_chars)
            if fallback is not None:
                keep_spans = fallback_keep
                body_spans = _subtract([fallback], keep_spans)
                if body_spans:
                    has_structure = True
    if not body_spans:
        # 命中预设但从结构上找不到正文区：有结构 ⇒ 判定「不可改，整条透传」，
        # 避免回退到通用保护层后把规划/状态栏当正文改写（11-13 事故）；
        # 完全没有该预设的结构 ⇒ 返回 None，让调用方走通用保护层。
        if has_structure:
            return protection.Plan(parts=[], skipped=True,
                                   reason="预设结构不完整，未找到正文区",
                                   prose_chars=0, total_chars=len(text))
        return None

    # 按位置把 keep 块和正文区拼回去；两者之间的零碎文本一律保留。
    events: list[tuple[int, int, str]] = [(s, e, "keep") for s, e in keep_spans]
    events += [(s, e, "prose") for s, e in body_spans]
    events.sort()

    parts: list[protection.Part] = []
    cursor = 0
    prose_chars = 0
    prose_segments = 0
    for start, end, kind in events:
        if start > cursor:
            parts.append(protection.Part("keep", text[cursor:start]))
        if kind == "keep":
            parts.append(protection.Part("keep", text[start:end]))
        else:
            region = text[start:end]
            sub_parts = _prose_parts(region, protection_cfg, min_rewrite_chars)
            for part in sub_parts:
                parts.append(part)
                if part.kind == "prose" and len(part.text.strip()) >= min_rewrite_chars:
                    prose_chars += len(part.text.strip())
                    prose_segments += 1
        cursor = max(cursor, end)
    if cursor < len(text):
        parts.append(protection.Part("keep", text[cursor:]))

    if prose_chars <= 0:
        return protection.Plan(parts=[], skipped=True,
                               reason="预设结构里没有可改写的正文（可能是截断输出）",
                               prose_chars=0, total_chars=len(text))
    return protection.Plan(parts=parts, skipped=False, reason="",
                           prose_chars=prose_chars, total_chars=len(text))


def describe(spec: PresetSpec) -> dict:
    """给日志/审计用的摘要。"""
    return {"id": spec.id, "name": spec.name,
            "keep_tags": list(spec.keep_tags),
            "body_tags": list(spec.body_tags),
            "plain_body_after": list(spec.plain_body_after),
            "plain_body_before": list(spec.plain_body_before)}
