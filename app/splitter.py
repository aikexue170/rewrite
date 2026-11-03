"""分句与【分数】标记拼接。

口径必须和 jev 站点/JEV 训练时完全一致：分句用 app/sentences.py（从
revnovel_next/jevd/sentences.py 原样拷来，SENT_RULES=3：丢章节标记 + <=12 字的
短句往后并），标记接法照抄 autotavern 打分脚本：

    units = segment_lines(text.split("\n"))
    每句 = unit + "【0.123】"（没有分数就是 "【NA】"）
    按段落号重新拼回，段落之间用 "\\n" 连接

拼出来的就是模型训练时见到的输入格式，不要自由发挥。
"""
from . import sentences


def split_units(text: str) -> list[tuple[int, str]]:
    """把一段正文切成待打分的单元 → [(段落号, 文本), ...]。"""
    return sentences.segment_lines(text.split("\n"), stem="")


def build_marked(text: str, scores: list[float | None]) -> str:
    """把单元和分数拼回带【】标记的输入文本。scores 与 split_units 结果一一对应。"""
    units = split_units(text)
    parts: dict[int, list[str]] = {}
    order: list[int] = []
    for (para, unit), score in zip(units, scores):
        if para not in parts:
            parts[para] = []
            order.append(para)
        mark = "【NA】" if score is None else f"【{score:.3f}】"
        parts[para].append(unit + mark)
    return "\n".join("".join(parts[p]) for p in order)


def split_body_chunks(text: str, limit: int) -> list[str]:
    """长正文按段落切片，避免单次改文输入过长（CoT + 正文超 max_tokens）。"""
    if limit <= 0 or len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for para in text.split("\n"):
        if cur and cur_len + len(para) + 1 > limit:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
        while len(para) > limit:          # 超长单段硬切
            chunks.append(para[:limit])
            para = para[limit:]
        cur.append(para)
        cur_len += len(para) + 1
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()]
