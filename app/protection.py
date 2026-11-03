"""结构化内容保护：把云端回复切成「可以改写的纯正文」和「必须原样保留的结构块」。

前端卡（酒馆助手 / MVU / 状态栏 / 正则卡）的回复里混着 HTML、代码块、变量更新块、
状态栏占位符、<user> 宏这些东西。改文模型会把它们当普通文本改写，标签名、JSON、
占位符全会被改坏，所以送改文之前先切出来，只改纯正文，结构块一个字节不动。

判断规则（保守优先，认不准就整条不改）：

1. 包装标签 <maintext>…</maintext>、<content>…</content>：标签本身保留，里面的正文照改。
2. 结构标签 div/style/script/table/details/<StatusBlock>/<UpdateVariable>/<SceneInfo> 等：
   整块保护，不参与改写。
3. 占位符/空标签 <StatusPlaceHolderImpl/>、<img> 标记：标签本身保留。
4. 行内标签（<br>/<font>/<span>）或 {{user}}、<user> 宏出现在某一段：那一段整段保留，
   其余段落照改。
5. 行级状态栏（尾部连成一片的 `key：value`、`- ` 列表、`【…】` 小标题）：识别成结构块保留。
6. 正文占比太低 / 正文被切得太碎 → 整条不改。
"""
import re
from dataclasses import dataclass, field

# 只包正文的标签：标签留着，内容照改
WRAPPER_TAGS = {
    "maintext", "content", "text", "narrative", "story", "message",
    "messagecontent", "reply",
}

# 行内标签：出现在某段里 → 那一段整段不改
INLINE_TAGS = {
    "br", "hr", "font", "span", "b", "i", "u", "s", "p", "a", "small", "big",
    "center", "em", "strong", "sub", "sup", "code", "ruby", "rt", "mark",
}

# 宏标签：和行内标签一样处理
MACRO_TAGS = {"user", "char", "bot"}

# 结构标签：整块保护
PROTECT_TAGS = {
    "html", "head", "body", "div", "style", "script", "table", "thead", "tbody",
    "tr", "td", "th", "details", "summary", "img", "button", "input", "select",
    "option", "options", "canvas", "svg", "form", "iframe", "video", "audio",
    "statusblock", "status_block", "status", "sceneinfo", "scene", "scene_status",
    "char_status", "character_status", "visual_cards", "plot", "affection",
    "place", "time", "selection", "updatevariable", "initvar", "analysis",
    "jsonpatch", "displaypatch", "update", "var", "variable",
}

# 占位符：只保护标签本身
PLACEHOLDER_TAGS = {"statusplaceholderimpl", "statusplaceholder"}

# 未知标签的归类：先看名字关键词，判不了再看内容特征
WRAP_KEYWORDS = {
    "正文", "正文内容", "正文部分", "叙事", "故事", "小说", "回复", "内容", "输出", "台词",
    "story", "content", "text", "narrative", "roleplay", "reply", "message", "output",
    "prose", "novel", "chapter",
}
STRUCT_KEYWORDS = {
    "状态", "面板", "选项", "更新", "变量", "数值", "好感", "时间", "地点", "场景", "剧情",
    "人物", "角色", "列表", "背包", "任务", "系统",
    "status", "state", "stats", "stat", "data", "update", "patch", "option", "choice",
    "select", "analysis", "plan", "summary", "detail", "plot", "scene", "time", "place",
    "world", "character", "relationship", "affection", "inventory", "bag", "task",
    "facility", "system", "sys", "variable", "json", "html", "style", "script", "table",
    "memo", "note", "log", "brain", "thought", "think", "cot", "draft", "suggest",
    "branch", "grade", "love", "self", "squad", "companion", "undercurrent",
    "equipment", "party", "guide", "rule", "setting", "config", "profile", "panel",
    "bar", "display", "map", "quest", "item", "skill", "buff", "debuff", "pic",
    "image", "img", "estado", "history", "memory", "event", "weather", "level",
    "money", "gold", "hp", "mp", "exp",
}

MACRO_RE = re.compile(r"\{\{[^{}\n]{1,80}\}\}")
TAG_RE = re.compile(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9_:.\-]*)([^<>]*?)(/?)>", re.S)
CJK_TAG_RE = re.compile(r"<\s*(/?)\s*([\u4e00-\u9fff]{1,6})\s*>")
FENCE_RE = re.compile(r"```.*?(?:```|$)", re.S)
COMMENT_RE = re.compile(r"<!--.*?(?:-->|$)", re.S)
KV_LINE_RE = re.compile(r"^\s*[^\n：:]{1,14}[：:]\s*\S")
TITLE_LINE_RE = re.compile(r"^\s*(?:【[^】]{1,12}】|『[^』]{1,30}』|#{1,4}\s)")
BRACKET_LINE_RE = re.compile(r"^\s*\[[^\[\]\n]{3,120}\]\s*$")
# 结尾选项块：标题行 + A./1./① 选项行
OPTION_HEAD_RE = re.compile(
    r"^\s*(?:结尾选项|选项|选择|选项如下|请选择|你的选择|接下来(?:的)?(?:选项|选择)|"
    r"剧情选项|行动选项|可选行动)\s*[:：]?\s*$")
OPTION_LINE_RE = re.compile(
    r"^\s*(?:[A-Za-z]|[0-9]{1,2}|[①②③④⑤⑥⑦⑧⑨⑩])\s*[.、．:：)）]\s*\S")
PAREN_LINE_RE = re.compile(r"^\s*[（(][^（）()\n]{2,60}[）)]\s*$")
BULLET_LINE_RE = re.compile(r"^\s*(?:[-*•·▸▶]\s+|🧑|👩|👨|⏰|📍|📅|💭|🎭|❤|💔|💦|🔥)")
PARA_SPLIT_RE = re.compile(r"(\n[ \t]*\n+)")


@dataclass
class Part:
    kind: str          # "prose"（可以改） | "keep"（原样保留）
    text: str


@dataclass
class Plan:
    parts: list[Part] = field(default_factory=list)
    skipped: bool = False
    reason: str = ""
    prose_chars: int = 0
    total_chars: int = 0


# ---------------------------------------------------------------- 结构扫描

def _find_matching(matches: list[re.Match], i: int, name: str) -> int:
    """从 matches[i]（开标签）找配对闭标签下标；没有返回 -1。"""
    depth = 1
    j = i + 1
    while j < len(matches):
        closing, n2 = bool(matches[j].group(1)), matches[j].group(2).lower()
        if n2 == name:
            depth += -1 if closing else 1
            if depth == 0:
                return j
        j += 1
    return -1


def _scan_structure(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]],
                                        list[int], list[int]]:
    """扫描结构。

    Returns: (保护块 span 列表, 原样保留的标签 token 列表, 行内标签位置列表, 宏位置列表)
    """
    matches = [m for m in TAG_RE.finditer(text)]
    cjk_matches = [m for m in CJK_TAG_RE.finditer(text)]
    all_matches = sorted(matches + cjk_matches, key=lambda m: m.start())
    protected: list[tuple[int, int]] = []
    keep_tokens: list[tuple[int, int]] = []
    inline_pos: list[int] = []
    macro_pos: list[int] = []

    consumed: set[int] = set()
    i = 0
    while i < len(all_matches):
        if i in consumed:
            i += 1
            continue
        m = all_matches[i]
        closing, name = bool(m.group(1)), m.group(2).lower()
        selfclose = bool(m.group(4)) if len(m.groups()) >= 4 else False

        if closing:                                  # 孤立闭标签：保留标签
            keep_tokens.append((m.start(), m.end()))
            i += 1
            continue
        if name in MACRO_TAGS:
            macro_pos.append(m.start())
            i += 1
            continue
        if name in INLINE_TAGS:
            inline_pos.append(m.start())
            i += 1
            continue
        if name in PLACEHOLDER_TAGS or selfclose:
            keep_tokens.append((m.start(), m.end()))
            i += 1
            continue

        j = _find_matching(all_matches, i, name)
        if name in WRAPPER_TAGS:
            if j < 0:                                # 包装标签没闭合：保护到结尾
                protected.append((m.start(), len(text)))
                i = len(all_matches)
                continue
            keep_tokens.append((m.start(), m.end()))
            keep_tokens.append((all_matches[j].start(), all_matches[j].end()))
            consumed.add(j)
            i += 1                                   # 继续处理内部标签
            continue
        if name in PROTECT_TAGS:
            # 已知结构标签：整块保护
            end = all_matches[j].end() if j >= 0 else len(text)
            protected.append((m.start(), end))
            i = (j + 1) if j >= 0 else len(all_matches)
            continue

        # 未知标签：按名字关键词 + 内容特征归类
        if j < 0:
            rest = text[m.end():m.end() + 240]
            if _name_is(name, STRUCT_KEYWORDS) and _looks_like_code(rest):
                protected.append((m.start(), len(text)))     # 疑似变量/JSON 块没闭合
                i = len(all_matches)
            else:
                keep_tokens.append((m.start(), m.end()))     # 其他没闭合的当标记，不吞正文
                i += 1
            continue
        inner = text[m.end():all_matches[j].start()]
        if _classify_unknown(name, inner) == "wrapper":
            keep_tokens.append((m.start(), m.end()))
            keep_tokens.append((all_matches[j].start(), all_matches[j].end()))
            consumed.add(j)
            i += 1                                           # 继续处理里面
        else:
            protected.append((m.start(), all_matches[j].end()))
            i = j + 1

    return _merge(protected), _merge(keep_tokens), inline_pos, macro_pos


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    spans = sorted(spans)
    out = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


# ---------------------------------------------------------------- 正文块处理

def _name_tokens(name: str) -> list[str]:
    parts = re.split(r"[^0-9a-z\u4e00-\u9fff]+", name.lower())
    return [p for p in parts if p]


def _name_is(name: str, keywords: set[str]) -> bool:
    """标签名里有没有这些关键词。英文词按词首匹配（history 不会命中 story），
    中文词按包含匹配。"""
    for token in _name_tokens(name) + [name]:
        for kw in keywords:
            if kw.isascii():
                if token.startswith(kw) or token == kw:
                    return True
            elif kw in token:
                return True
    return False


def _looks_like_prose(inner: str) -> bool:
    """这段内容像不像小说正文：够长、中文为主、句末标点多、或对白引号多、代码符号少。"""
    text = inner.strip()
    if len(text) < 120:
        return False
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return False
    cjk = sum(1 for c in chars if "\u4e00" <= c <= "\u9fff") / len(chars)
    punct = sum(text.count(p) for p in "。！？…")
    dialogue = sum(text.count(p) for p in "“”「」『』")
    code = sum(1 for c in chars if c in "{}[]();:=|<>\\\"`") / len(chars)
    lines = [ln for ln in text.split("\n") if ln.strip()]
    kv = sum(1 for ln in lines if KV_LINE_RE.match(ln))
    if len(lines) >= 3 and kv / len(lines) >= 0.35 and dialogue < 2:
        return False                                   # 更像状态栏
    if code >= 0.08:
        return False                                   # 代码/JSON 味太重
    return (cjk >= 0.5 and punct >= 3) or dialogue >= 2


def _classify_unknown(name: str, inner: str) -> str:
    """未知标签 → 'wrapper'（标签留着、内容改）或 'protect'（整块保留）。"""
    if _name_is(name, WRAP_KEYWORDS):
        return "wrapper"
    if _name_is(name, STRUCT_KEYWORDS):
        return "protect"
    if _looks_like_prose(inner):
        return "wrapper"
    return "protect"


def _looks_like_code(text: str) -> bool:
    head = text.lstrip()[:200]
    if not head:
        return False
    return head[0] in "{[\"" or head.startswith("```") or head.count("{") >= 2


def _is_status_line(line: str) -> bool:
    return bool(KV_LINE_RE.match(line) or TITLE_LINE_RE.match(line)
                or BULLET_LINE_RE.match(line) or BRACKET_LINE_RE.match(line))


def _split_trailing_options(text: str) -> tuple[str, str]:
    """把正文尾部连成一片的「结尾选项」切出来（选项标题 或 ≥2 个选项行）。"""
    lines = text.split("\n")
    idx = len(lines)
    header_found = False
    option_lines = 0
    while idx > 0:
        line = lines[idx - 1]
        if not line.strip():
            idx -= 1
            continue
        if OPTION_HEAD_RE.match(line):
            header_found = True
            idx -= 1
            continue
        if OPTION_LINE_RE.match(line) or PAREN_LINE_RE.match(line):
            option_lines += 1
            idx -= 1
            continue
        break
    block = lines[idx:]
    if not (header_found or option_lines >= 2):
        return text, ""
    non_empty = [ln for ln in block if ln.strip()]
    if len(non_empty) < 2 or len(non_empty) > 20:
        return text, ""
    if idx == 0:
        return "", text
    return "\n".join(lines[:idx]), "\n" + "\n".join(block)


def _split_leading_status(text: str) -> tuple[str, str]:
    """把开头连成一片的状态行（[日期|时间|地点]、key：value 之类）切出来（至少 2 行）。"""
    lines = text.split("\n")
    idx = 0
    while idx < len(lines) and (not lines[idx].strip() or _is_status_line(lines[idx])):
        idx += 1
    block = lines[:idx]
    status_lines = [ln for ln in block if _is_status_line(ln)]
    if len(status_lines) < 2:
        return "", text
    return "\n".join(block), "\n".join(lines[idx:])


def _split_trailing_status(text: str) -> tuple[str, str]:
    """把一段尾部连成一片的状态行切出来（至少 3 行、且过半是状态行才动）。"""
    lines = text.split("\n")
    idx = len(lines)
    while idx > 0 and (not lines[idx - 1].strip() or _is_status_line(lines[idx - 1])):
        idx -= 1
    block = lines[idx:]
    status_lines = [ln for ln in block if _is_status_line(ln)]
    non_empty = [ln for ln in block if ln.strip()]
    if len(status_lines) < 3 or len(status_lines) * 2 < len(non_empty):
        return text, ""
    if idx == 0:
        return "", text
    return "\n".join(lines[:idx]), "\n" + "\n".join(block)


def _region_parts(region: str, offset: int, inline_pos: list[int],
                  macro_pos: list[int]) -> list[Part]:
    """处理两段结构之间的区域：按空行分段，含行内标签/宏的段保留，其余当正文。"""
    if not region.strip():
        return [Part("keep", region)]
    mark_positions = inline_pos + macro_pos
    blocks = PARA_SPLIT_RE.split(region)      # [block, sep, block, sep, ...]
    parts: list[Part] = []
    prose_buf = ""

    def flush_prose() -> None:
        nonlocal prose_buf
        if prose_buf:
            parts.append(Part("prose", prose_buf))
            prose_buf = ""

    pos = offset
    idx = 0
    while idx < len(blocks):
        block = blocks[idx]
        if idx % 2 == 1:                       # 分段分隔符：跟着前一段走
            idx += 1
            continue
        sep = blocks[idx + 1] if idx + 1 < len(blocks) else ""
        block_end = pos + len(block)
        has_mark = any(pos <= p < block_end for p in mark_positions)
        if has_mark:
            flush_prose()
            parts.append(Part("keep", block + sep))
        else:
            prose_buf += block + sep
        pos = block_end + len(sep)
        idx += 2

    flush_prose()
    # 正文段内部再切开头/尾部的纯文本状态栏
    out: list[Part] = []
    for part in parts:
        if part.kind != "prose":
            out.append(part)
            continue
        status_head, rest = _split_leading_status(part.text)
        if status_head:
            out.append(Part("keep", status_head))
            part = Part("prose", rest if rest else "")
        body_text, option_tail = _split_trailing_options(part.text)
        if option_tail:
            out.append(Part("prose", body_text))
            out.append(Part("keep", option_tail))
            continue
        body_text, status_tail = _split_trailing_status(part.text)
        if status_tail:
            out.append(Part("prose", body_text))
            out.append(Part("keep", status_tail))
        else:
            out.append(part)
    return out


# ---------------------------------------------------------------- 主入口

def plan_reply(text: str, *, min_prose_ratio: float = 0.15, max_segments: int = 4,
               min_rewrite_chars: int = 60) -> Plan:
    """把一条模型回复切成可改写的正文段和原样保留的结构段。"""
    total = len(text)
    result = Plan(total_chars=total)
    if not text.strip():
        result.skipped = True
        result.reason = "空回复"
        return result

    protected, keep_tokens, inline_pos, macro_pos = _scan_structure(text)
    protected += [m.span() for m in FENCE_RE.finditer(text)]
    protected += [m.span() for m in COMMENT_RE.finditer(text)]
    protected = _merge(protected)
    macro_pos += [m.start() for m in MACRO_RE.finditer(text)]

    boundaries = _merge(protected + keep_tokens)
    parts: list[Part] = []
    cursor = 0
    for s, e in boundaries:
        if s > cursor:
            parts.extend(_region_parts(text[cursor:s], cursor, inline_pos, macro_pos))
        parts.append(Part("keep", text[s:e]))
        cursor = max(cursor, e)
    if cursor < len(text):
        parts.extend(_region_parts(text[cursor:], cursor, inline_pos, macro_pos))

    result.parts = parts
    prose_parts = [p for p in parts if p.kind == "prose" and len(p.text.strip()) >= min_rewrite_chars]
    prose_chars = sum(len(p.text.strip()) for p in prose_parts)
    result.prose_chars = prose_chars

    if not prose_parts:
        result.skipped = True
        result.reason = "没有够长的纯正文段"
        return result
    if prose_chars / max(1, total) < min_prose_ratio:
        result.skipped = True
        result.reason = f"正文占比过低（{prose_chars / max(1, total):.0%}）"
        return result
    if len(prose_parts) > max_segments:
        result.skipped = True
        result.reason = f"正文被切得太碎（{len(prose_parts)} 段）"
        return result
    return result
