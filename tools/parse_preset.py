#!/usr/bin/env python3
"""从 SillyTavern Chat Completion 预设 JSON 生成「预设解析 spec」草稿。

路由器运行期只读 ``preset_specs/*.json``；本工具负责把 ST 预设转成草稿，人工复核
结构规则后放进 ``preset_specs/`` 即可上线（不重启，目录 mtime 变了自动重载）。

用法::

    # 单份：生成草稿到 stdout（或 --out 写文件）
    python3 tools/parse_preset.py 你的预设定/某预设.json

    # 批量：整个预设定目录 → 输出到 preset_specs/
    python3 tools/parse_preset.py --presets-dir 预设/ --out-dir preset_specs/

    # 校验已有 spec 仓库
    python3 tools/parse_preset.py --check preset_specs/

自动能推的：名称/来源、正文可疑标签、保护标签、正则脚本清单、酒馆助手脚本清单、
跨预设唯一的提示词长句指纹。**推不准的**（锚点、固定前缀、body_is_whole）会在
``notes`` 里标 TODO，需要人工按 ``preset_specs/README.md`` 补齐。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# 正文嫌疑标签名（含这些词的标签更可能是正文容器）
BODY_HINTS = ("body", "maintext", "main_text", "content", "narrative",
              "story", "message", "reply", "text", "prose", "novel")
# 结构嫌疑标签名（守护：规划/场景/状态/面板/选项/摘要/变量）
KEEP_HINTS = ("think", "thought", "analysis", "plan", "scene", "status", "state",
              "summary", "memo", "memory", "option", "choice", "panel", "details",
              "self_check", "selfcheck", "check", "var", "update", "patch",
              "discuss", "answer", "plot", "history", "setting", "profile",
              "after_format", "after_thinking", "parallel", "event", "review",
              "draft", "suggest", "tips", "notes", "note", "list", "card",
              "brain", "cot", "reasoning", "meta", "info", "data")
# HTML 常规标签：不当预设特征
HTML_TAGS = {"div", "span", "style", "script", "br", "hr", "p", "a", "img",
             "details", "summary", "table", "tr", "td", "th", "thead", "tbody",
             "html", "head", "body", "font", "b", "i", "u", "s", "em", "strong",
             "small", "big", "center", "code", "pre", "blockquote", "ul", "ol",
             "li", "h1", "h2", "h3", "h4", "h5", "h6", "canvas", "svg", "button",
             "input", "select", "option", "iframe", "video", "audio", "form"}
TAG_RE = re.compile(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9_.:\-]*)\b([^<>]*?)(/?)>")
CLASS_RE = re.compile(r'class\s*=\s*["\']([^"\']+)["\']')
FIXED_PREFIX_HINTS = ("好的，我将", "好的，以下是", "以下是", "当然，以下是")


def slug(text: str) -> str:
    """文件名 → 稳定 id（中文转拼音不现实，用「可读前缀 + 短哈希」）。"""
    base = Path(text).stem
    ascii_part = re.sub(r"[^0-9A-Za-z]+", "-", base).strip("-").lower()
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
    if ascii_part:
        return f"{ascii_part[:24]}-{digest}" if len(ascii_part) > 24 else ascii_part
    return f"preset-{digest}"


def load_preset(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"预设不是对象：{path}")
    return payload


def preset_texts(preset: dict) -> list[tuple[str, str]]:
    """[(标识, 提示词文本)]，只取启用条目，保留顺序。"""
    prompts = preset.get("prompts") or []
    if not isinstance(prompts, list):
        return []
    out: list[tuple[str, str]] = []
    for item in prompts:
        if not isinstance(item, dict):
            continue
        if item.get("enabled") is False:
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        ident = str(item.get("identifier") or item.get("name") or "")
        out.append((ident, content))
    return out


def collect_tag_names(preset: dict) -> dict[str, int]:
    """预设里出现过的标签及次数（来自提示词正文 + 正则 findRegex）。"""
    counts: dict[str, int] = {}
    for _ident, content in preset_texts(preset):
        for match in TAG_RE.finditer(content):
            name = match.group(2).lower()
            if name in HTML_TAGS:
                continue
            counts[name] = counts.get(name, 0) + 1
    for script in (preset.get("extensions") or {}).get("regex_scripts") or []:
        if not isinstance(script, dict):
            continue
        for field in ("findRegex", "replaceString"):
            value = script.get(field)
            if not isinstance(value, str):
                continue
            for match in TAG_RE.finditer(value):
                name = match.group(2).lower()
                if name in HTML_TAGS:
                    continue
                counts[name] = counts.get(name, 0) + 1
    return counts


def collect_display_markers(preset: dict) -> list[str]:
    """美化正则（markdownOnly）里生成的 class 名，作为「显示态」匹配标记。"""
    markers: list[str] = []
    for script in (preset.get("extensions") or {}).get("regex_scripts") or []:
        if not isinstance(script, dict):
            continue
        if not script.get("markdownOnly") or script.get("disabled"):
            continue
        replace = script.get("replaceString")
        if not isinstance(replace, str):
            continue
        for match in CLASS_RE.finditer(replace):
            for cls in match.group(1).split():
                if cls and cls not in markers and len(cls) >= 3:
                    markers.append(cls)
    return markers


def classify_tags(counts: dict[str, int]) -> tuple[list[str], list[str], list[str]]:
    """标签按名字分到 (body, wrapper, keep)；body/wrapper 都保留标签、内容当正文。"""
    body: list[str] = []
    wrapper: list[str] = []
    keep: list[str] = []
    for name in sorted(counts):
        hit_body = any(h in name for h in BODY_HINTS)
        hit_keep = any(h in name for h in KEEP_HINTS)
        if hit_body and not hit_keep:
            (body if ("body" in name or "maintext" in name or "main_text" in name)
             else wrapper).append(name)
        elif hit_body and hit_keep:
            # 名字同时有正文和结构词（如 xxx_body_status）→ 保守：保护
            keep.append(name)
        elif hit_keep:
            keep.append(name)
    return body, wrapper, keep


def fingerprints(preset: dict, other_texts: list[str], limit: int = 8) -> list[str]:
    """挑跨预设唯一的提示词长句（≥20 字、不含宏/标签/标题行）。"""
    candidates: list[str] = []
    seen: set[str] = set()
    for ident, content in preset_texts(preset):
        if ident in ("main", "chatHistory", "dialogueExamples"):
            continue
        for line in re.split(r"\n+", content):
            line = line.strip()
            if not (24 <= len(line) <= 120):
                continue
            if "{{" in line or "<" in line and ">" in line:
                continue
            if line.startswith(("#", "-", "*", "|", ">", "[", "「")):
                continue
            if not re.search(r"[\u4e00-\u9fff]", line):
                continue
            if sum(1 for ch in line if "\u4e00" <= ch <= "\u9fff") < 12:
                continue
            key = line[:24]
            if key in seen:
                continue
            seen.add(key)
            candidates.append(line)
    # 去掉在别的预设里也出现的句子（区分度）
    out: list[str] = []
    for line in candidates:
        if any(line in other for other in other_texts):
            continue
        out.append(line)
        if len(out) >= limit:
            break
    return out


def build_spec(preset: dict, *, source_file: str, other_texts: list[str],
               preset_id: str | None = None) -> dict:
    """生成 spec 草稿（结构锚点等需人工复核，notes 里标 TODO）。"""
    counts = collect_tag_names(preset)
    body, wrapper, keep = classify_tags(counts)
    markers = [f"<{name}>" for name in (body + wrapper + keep)[:6]]
    markers += collect_display_markers(preset)[:4]

    regex_scripts = []
    for script in (preset.get("extensions") or {}).get("regex_scripts") or []:
        if not isinstance(script, dict) or script.get("disabled"):
            continue
        find = script.get("findRegex")
        replace = script.get("replaceString")
        regex_scripts.append({
            "name": script.get("scriptName"),
            "find": (str(find)[:160] + "…") if find and len(str(find)) > 160 else find,
            "replace": (str(replace)[:60] + "…") if replace and len(str(replace)) > 60 else replace,
            "placement": script.get("placement"),
            "markdown_only": bool(script.get("markdownOnly")),
            "prompt_only": bool(script.get("promptOnly")),
        })

    tavern_scripts = []
    helper = ((preset.get("extensions") or {}).get("tavern_helper") or {})
    for script in helper.get("scripts") or []:
        if not isinstance(script, dict):
            continue
        if script.get("enabled") is False:
            continue
        tavern_scripts.append({"name": script.get("name"),
                               "role": "待人工判断（看 info/正则/事件钩子）"})

    fixed_prefix = ""
    for _ident, content in preset_texts(preset):
        for line in content.splitlines():
            line = line.strip()
            for hint in FIXED_PREFIX_HINTS:
                if line.startswith(hint) and len(line) <= 40:
                    fixed_prefix = line
                    break
            if fixed_prefix:
                break
        if fixed_prefix:
            break

    # 场景类标签（如 dream_scene）的闭标签适合当「正文起点」锚点
    scene_tags = [name for name in keep
                  if any(h in name for h in ("scene", "状态栏", "状态"))]
    after = [f"</{name}>" for name in scene_tags[:2]]
    before = [f"<{name}>" for name in keep
              if any(h in name for h in ("after_format", "option", "panel", "summary"))][:3]

    notes = ("自动生成草稿：正文抽取规则（body_tags/plain_body/keep_prefixes/"
             "body_is_whole）与指纹需人工复核后再上线。"
             f"自动识别标签 {len(counts)} 个。")
    if not body and not wrapper:
        notes += " 未发现正文嫌疑标签：考虑 body_is_whole=true 或手工加 plain_body 锚点。"

    spec = {
        "id": preset_id or slug(source_file),
        "name": Path(source_file).stem,
        "source_file": source_file,
        "match": {"fingerprints": fingerprints(preset, other_texts),
                  "markers": markers, "min_markers": 1, "min_text_score": 8},
        "structure": {"keep_tags": keep, "wrapper_tags": wrapper, "body_tags": body,
                      "plain_body": {"after": after, "before": before},
                      "keep_prefixes": [fixed_prefix] if fixed_prefix else [],
                      "body_is_whole": False},
        "regex_scripts": regex_scripts,
        "tavern_helper_scripts": tavern_scripts,
        "notes": notes,
    }
    return spec


def all_preset_texts(presets: list[tuple[Path, dict]]) -> list[list[str]]:
    """每个预设的全部提示词文本（用于跨预设去重指纹）。"""
    return ["\n".join(content for _ident, content in preset_texts(preset))
            for _path, preset in presets]


def parse_one(path: Path, other_texts: list[str],
              preset_id: str | None = None) -> dict:
    return build_spec(load_preset(path), source_file=path.name,
                      other_texts=other_texts, preset_id=preset_id)


# ---------------------------------------------------------------- 校验


def validate_spec(payload: dict, where: str,
                  warnings: list[str] | None = None) -> list[str]:
    problems: list[str] = []
    if not isinstance(payload, dict):
        return [f"{where}: 不是 JSON 对象"]
    if not payload.get("id"):
        problems.append(f"{where}: 缺 id")
    match = payload.get("match") or {}
    for fp in match.get("fingerprints") or []:
        text = str(fp.get("text") if isinstance(fp, dict) else fp)
        if len(text) < 20 and warnings is not None:
            warnings.append(f"{where}: 指纹过短（<20 字，运行时会被忽略）：{text[:20]}")
    structure = payload.get("structure") or {}
    has_body = bool(structure.get("body_tags") or structure.get("wrapper_tags")
                    or structure.get("plain_body") or structure.get("body_is_whole"))
    if not has_body:
        problems.append(f"{where}: 没有任何正文规则（body_tags/wrapper_tags/"
                        "plain_body/body_is_whole）")
    if not (match.get("fingerprints") or match.get("markers")):
        problems.append(f"{where}: 匹配指纹和标记都为空，永远匹配不上")
    return problems


def check_dir(directory: Path) -> int:
    problems: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for file in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{file}: 读取失败 {exc}")
            continue
        problems.extend(validate_spec(payload, str(file), warnings))
        spec_id = str(payload.get("id") or "")
        if spec_id in seen:
            problems.append(f"{file}: id 重复 {spec_id}")
        seen.add(spec_id)
    for warning in warnings:
        print(f"警告：{warning}")
    if problems:
        print("\n".join(problems))
        return 1
    print(f"{len(list(directory.glob('*.json')))} 份 spec 校验通过：{directory}")
    return 0


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("preset", nargs="?", help="单份 ST 预设 JSON")
    parser.add_argument("--presets-dir", help="预设目录（批量模式）")
    parser.add_argument("--out", help="草稿输出文件（默认 stdout）")
    parser.add_argument("--out-dir", help="批量模式输出目录（默认 preset_specs）")
    parser.add_argument("--id", help="指定 spec id（默认从文件名生成）")
    parser.add_argument("--check", help="校验一个 spec 目录后退出")
    args = parser.parse_args(argv)

    if args.check:
        return check_dir(Path(args.check))

    if args.presets_dir:
        preset_dir = Path(args.presets_dir)
        out_dir = Path(args.out_dir or "preset_specs")
        out_dir.mkdir(parents=True, exist_ok=True)
        files = sorted(preset_dir.glob("*.json"))
        pairs = [(file, load_preset(file)) for file in files]
        texts = all_preset_texts(pairs)
        for index, (file, preset) in enumerate(pairs):
            others = [text for j, text in enumerate(texts) if j != index]
            spec = build_spec(preset, source_file=file.name, other_texts=others)
            target = out_dir / f"{spec['id']}.json"
            target.write_text(json.dumps(spec, ensure_ascii=False, indent=2),
                              encoding="utf-8")
            print(f"生成 {target}（指纹 {len(spec['match']['fingerprints'])} 条）")
        return 0

    if not args.preset:
        parser.error("需要预设文件，或 --presets-dir / --check")
    path = Path(args.preset)
    spec = parse_one(path, [], preset_id=args.id)
    text = json.dumps(spec, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"写入 {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
