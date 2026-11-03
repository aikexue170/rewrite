#!/usr/bin/env python3
"""分句与「章节标记」判定 —— 从原 data_prep/label_sentences.py 原样搬过来。

这两件事是**运行时**要用的：server.py 用它切书库正文，jev.py 用同一套规则切评测场
的输入（保证评测场和标注页看到的分句完全一致）。原来放在 data_prep/ 里靠 sys.path
hack 导入，2026-09-26 整理目录时搬进 marker/，逻辑一个字符没改。

data_prep/label_sentences.py 的其余部分（语料遍历、交互标注、日志回放）是旧桌面标注
工具的东西，随 data_prep/ 一起删掉了。
"""
import collections
import re

# 下面两段是从 rules.py 抄过来的默认提问与标准：登记在 registry.json 里的模型版本
# 会带自己的 instructions/criteria，这里只是没带时的兜底。
DEFAULT_INSTRUCTIONS = '下面这句话写得好还是不好？'

DEFAULT_CRITERIA = {
    '好': '断句阅读舒服，修辞和形容描写顺畅不冗杂，句子长度不会太短也不会太长，'
          '角色对话自然、口吻舒服且真实、不会出现频繁断句。',
    '不好': '频繁断句，很多短句，读起来破碎。修辞冗杂，繁复，堆砌形容词，'
            '句子长度极短或者极长，角色对话不自然、机械、频繁断句。',
}


# 「小灰聊天记录专区」的作品：小灰发布聊天时加在标题前的固定前缀。三处必须一致——
# agent.py 的 publish_chat（发布）、web/index.js 的 isChat（书库单独一张卡）、
# purity.py 的 is_chat_log（聊天流水不进「数据提纯」看板）。
CHAT_LOG_PREFIX = '小灰聊天·'

QUOTE_PAIRS = {'“': '”', '‘': '’', '「': '」', '『': '』', '（': '）', '(': ')'}
QUOTE_OPENERS = set(QUOTE_PAIRS)
QUOTE_CLOSERS = set(QUOTE_PAIRS.values())


def _quote_step(stack, char):
    """Track one quote character: open a level, close one, or shrug it off.

    Drafts are not consistent about quote characters — a line may open with “ and
    close with a straight ", or open a quote twice by mistake. So closers are
    taken at face value (they close the innermost level whatever their type),
    and a repeated opener is read as the closer it was probably meant to be.
    A stray closer with nothing open is ignored rather than swallowing the rest
    of the paragraph.
    """
    if char == '"':                       # ambiguous: closes if something is open
        if stack:
            stack.pop()
        else:
            stack.append('"')
    elif char in QUOTE_OPENERS:
        if stack and stack[-1] == QUOTE_PAIRS[char]:
            stack.pop()                   # “opened“ twice, second one closes
        else:
            stack.append(QUOTE_PAIRS[char])
    elif stack:                           # a closer, matched or not
        stack.pop()


def split_sentences(text):
    """Sentences of one document, in reading order.

    A sentence ends at 。 and keeps it, but only when that 。 sits outside
    quotation marks: `“你别走。”她说，声音很轻。` is one sentence, not two
    fragments with a dangling quote. Line breaks are boundaries too, so a
    paragraph never absorbs the next one just because it ends in ！ or in a
    closing quote instead of a full stop.
    """
    out = []
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        stack, start = [], 0
        for index, char in enumerate(line):
            if char == '。':
                if stack:
                    continue
                piece = line[start:index + 1].strip()
                if piece:
                    out.append(piece)
                start = index + 1
            elif char in QUOTE_OPENERS or char in QUOTE_CLOSERS or char == '"':
                _quote_step(stack, char)
        tail = line[start:].strip()
        if tail:
            out.append(tail)                      # unbalanced quotes end with the line
    return out


HEADING = re.compile(r'(序章|序|楔子|前言|尾声|终章|后记|番外|'
                     r'第[一二三四五六七八九十百千零〇0-9]{1,4}[章回节卷篇幕])'
                     r'([：:·、\s][^。]{0,20})?$')

# 规则版本号：**分句规则改了就必须 +1**，因为它同时是这些地方的失效键：
#   · server 的 list_cache（不然缓存里的默认档还是老规则算出来的）
#   · 分段配置 part_cfg（不然老分段配置配着新句号）
#   · 预扫描产物 .done.json（不然老概率配着新句号）
# ⚠️ **标注日志没有版本号**——改分句之前必须先迁移，不然全库的票静默错位。
#    v2 → v3（2026-09-27）：丢掉章节标记 + 把 <=MIN_UNIT_LEN 字的句子往后并。
SENT_RULES = 3

# 待标注单元的最小长度。**<=''MIN_UNIT_LEN'' 字的句子会并到后面，直到整组超过它。**
# 4 个字的句子没有任何可判断的东西：档位标准写死了「只看句子本身、不看上下文」，
# 而「频繁断句、很多短句」这种判据本来就是段落级的，句子级单位承载不了。
# 选 12 是先量过全库：<=10 字的句子占 16.4%、落在它们上面的票占 13.2%。
MIN_UNIT_LEN = 12

# 短行要「长得像标记」才算标记：纯数字/中文数字，或者这几个固定的标记词。
MARKER_WORDS = {'序', '序章', '序言', '楔子', '前言', '引子', '尾声', '终章', '后记', '番外',
                '目录', '附录', '启', '间', '上', '下', '中', '完', '全文完', 'TBC', 'END'}
NUMERAL = re.compile(r'^[0-9０-９一二三四五六七八九十百千零〇ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+$')
# （待写）（略）（一）（上）这种括号标记；不能再宽，否则「（很可能是后者）」也会中招
PAREN_MARKER = re.compile(r'^[（(]\s*(?:待写|待补|待定|略|略过|删除|未完|完|上|下|中|序|启|'
                          r'[0-9０-９一二三四五六七八九十百千零〇]{1,4})\s*[）)]$')


def _looks_like_marker(bare):
    """一个短行像不像「章节标记」而不是正文碎片。

    纯数字最长 4 位（章节号/页码的量级）。不限长的话，时间戳那种 13 位数字也会被当成标记
    ——实测《自称裸体艺术家的美少女上野川凛》里 114 个时间戳就是这么中招的。
    """
    if bare in MARKER_WORDS:
        return True
    return len(bare) <= 4 and bool(NUMERAL.fullmatch(bare))


def is_heading(text, stem):
    """True for the lines that are furniture rather than prose.

    Three kinds show up in the drafts: the title repeated at the top of the
    file, the chapter number on its own (一 / 二 / 启), and a chapter heading or
    byline (序章：罗塞塔之破, By 北棱). They are not sentences, and a label on one
    would teach the model nothing, so they are left out of the corpus entirely.

    Short is not the same as furniture: 枪响。 and 十岁。 are real sentences and
    the "too short" verdict is one of the things being labelled, so a line only
    counts as a marker when it has no sentence-ending punctuation at all.

    2026-09-26 收紧：以前「没有句末标点 + ≤2 字」一律算标记，硬换行的稿子（每行二三十字、
    换行点会把句子切开）会切出一堆 `她`/`当`/`窗`/`”` 这样的碎片，全被当成章节号默认跳过——
    标注的人看到「系统说这是标题」可是内容是半句话。《O的故事》251 个「标题」里有 146 个是
    这种碎片。现在那个短行还必须**长得像标记**（纯数字/中文数字，或 MARKER_WORDS 里的词），
    括号那条也收紧成 PAREN_MARKER。
    """
    body = text.strip()
    bare = body.strip('。')
    if bare in {stem, stem.strip('《》')}:                  # the title line
        return True
    if not re.search(r'[。！？…]', body) and _looks_like_marker(bare):
        return True                                          # 一 / 12 / 启 / 目录
    if PAREN_MARKER.fullmatch(bare):                         # （待写）（一）（上）
        return True
    if re.match(r'(?i)by[\s:：]', body):                     # By 北棱
        return True
    return len(bare) <= 24 and bool(HEADING.fullmatch(bare))


# 硬换行合并时用：这些字符结尾说明「这一行写完了」
LINE_DONE = '。！？…；;”’」』）】》>」'
# 这些字符结尾当成「写完了」：破折号/等号/星号多是分隔装饰，别把装饰行跟正文接起来
DECOR = '-—=*~_·'
# 看着像章节标题的行，前面不接
TITLE_LINE = re.compile(r'^(第\s*[一二三四五六七八九十百千零〇0-9]{1,4}\s*[章回节卷篇幕]|'
                        r'序章|序言|楔子|前言|引子|尾声|终章|后记|番外|目录|附录)')
# 一整行里有一长串同样的标点（`第 1 节-----`）＝分隔装饰，不参与合并
DECO_LINE = re.compile(r'[-—=*~_·]{8,}')


# 文件名尾巴上的来源标签（（人类写作）/（AIGC））——is_heading 要拿去掉标签的名字比书名。
CATEGORY_TAG = re.compile(r'（(?:人类写作|AIGC)）$')


def work_stem(filename_stem):
    return CATEGORY_TAG.sub('', filename_stem)


def _unit_weight(text):
    """算「几个字」：去掉首尾空白后的字符数（标点也算，和一般人说"这句子几个字"一致）。"""
    return len(text.strip())


def segment_lines(lines, stem='', min_len=MIN_UNIT_LEN):
    """把行切成**待标注单元** → [(段落号, 文本), ...]。这是全站唯一的切分口径。

    SENT_RULES=3（2026-09-27）起两条规则：

    1. **章节标记（is_heading）整句丢掉**。它本来就不是正文，占着句号位只能被打「跳过」，
       纯属浪费标注注意力。
    2. **长度 <= min_len 的句子往后并**，直到整组超过 min_len。4 个字的句子没法判断好坏，
       而"频繁断句、很多短句"是段落级的毛病，句子级单位根本承载不了。

    ⚠️ 第 2 条**必须跨行做**，这是量出来的（全库 47.6 万句）：

    | | 单元数 | 仍 <=12 字的单元 |
    |---|---|---|
    | 原样 | 475,673 | 22.4% |
    | **只在行内并** | 449,341 | **22.4%**（而且 100% 都是"整行本来就这么短"） |
    | **跨行并** | 376,720 | **3.0%** |

    网文的对白就是一行一句（「哦。」「到了。」），行内根本没有东西可并——所以只做行内
    合并等于没做。

    不跨过去的边界（免得把两场戏缝成一句）：
      · 空行；
      · 一整行都是章节标记的那一行（标记本身已经被丢掉，它是章节分界）。
    段落号取单元**第一句所在的行**，单调不减——标注页按它分组渲染（原文模式）。
    """
    out, seq = [], []

    def flush():
        nonlocal seq
        if not seq:
            return
        head = len(out)
        group, weight, first = [], 0, None
        for lineno, sentence in seq:
            if first is None:
                first = lineno
            group.append(sentence)
            weight += _unit_weight(sentence)
            if weight > min_len:
                out.append((first, ''.join(group)))
                group, weight, first = [], 0, None
        if group:                                  # 块尾的短句并回本块上一组
            text = ''.join(group)
            if len(out) > head:
                para, prev = out[-1]
                out[-1] = (para, prev + text)
            else:
                out.append((first, text))
        seq = []

    for index, line in enumerate(lines):
        raw = split_sentences(line)
        if not raw:
            flush()                                # 空行 = 块边界
            continue
        parts = [s for s in raw if not is_heading(s, stem)]
        if not parts:
            flush()                                # 整行都是章节标记 = 章节分界
            continue
        for sentence in parts:
            seq.append((index, sentence))
    flush()
    return out


def join_soft_wraps(text, min_ratio=0.6, bridge_blank=True):
    """把「硬换行」的稿子还原成段落。返回 (新正文, 合并掉的行数)。

    有些来源（网页抓取、某些导出工具）会把整本书折到固定列宽，换行点还会把句子从中间切开；
    这样分句之后会得到一堆 `她`、`当`、`窗`、`”` 那样的碎片（《O的故事》4757 句里有 485 句
    是 ≤3 字的碎片）。这里按「这一行看着还没写完」把它们接回去。

    **只在整份稿子确实是被折过行的时候动手**：非空行里有一半以上落在同一个宽度（±2 字）
    才算「硬换行」。一段一行的正常稿子（《十日谈》众数 1、集中度 4%）直接原样返回，
    一行都不合并——避免把正常的段落结构揉成一团。

    判定细节：
    - 上一行以句末标点、收尾引号/括号、或者破折号之类装饰结尾 → 写完了，不接；
    - 上一行短于 `max(中位行长×0.6, 众数宽度×0.85)` → 可能是对白或小标题，不接；
    - 下一行看着像章节标题 → 不接；
    - 单个空行 + 上一行是满宽且没写完（抓取稿连段落也从中间断）→ 接回去；
      连着两个以上空行才算真分段，绝不跨过去。

    **只用于新导入的正文**（主页上传 / 酒馆导入 / 小灰发布）。已经标过的老文件绝不能动：
    合并会改变句子序号，已有的票就全错位了。
    """
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = []
    for raw_line in text.split('\n'):        # PDF/抓取稿的分页噪音：分页符 + 孤立的页码行
        if '\x0c' in raw_line:
            bare = raw_line.replace('\x0c', '').strip()
            if bare.isdigit() and len(bare) <= 4:
                continue                      # 光秃秃一个页码，丢掉
            raw_line = raw_line.replace('\x0c', '')
        lines.append(raw_line)
    widths = sorted(len(l.strip()) for l in lines if l.strip())
    if len(widths) < 20:
        return text, 0
    median = widths[len(widths) // 2]
    if median < 12:
        return text, 0
    mode = collections.Counter(widths).most_common(1)[0][0]
    uniform = sum(1 for w in widths if abs(w - mode) <= 2) / len(widths)
    if uniform < 0.5:                         # 不是被折过行的稿子，别乱动
        return text, 0
    floor = max(median * min_ratio, mode * 0.85)
    paragraphs, buffer, joins, blanks = [], '', 0, 0

    def flush():
        nonlocal buffer
        if buffer:
            paragraphs.append(buffer)
            buffer = ''

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            blanks += 1
            if not buffer:
                continue
            if (blanks == 1 and bridge_blank and len(buffer) >= floor
                    and buffer[-1] not in LINE_DONE and buffer[-1] not in DECOR
                    and not DECO_LINE.search(buffer)):
                joins += 1                    # 软换行：接着往下接
                continue
            flush()
            continue
        blanks = 0
        if not buffer:
            buffer = line
            continue
        if (len(buffer) >= floor and buffer[-1] not in LINE_DONE and buffer[-1] not in DECOR
                and not DECO_LINE.search(line) and not DECO_LINE.search(buffer)
                and not TITLE_LINE.match(line)):
            buffer += line
            joins += 1
            continue
        flush()
        buffer = line
    flush()
    merged = '\n\n'.join(paragraphs).strip()
    return (merged + '\n' if merged else text), joins
