#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：模型把思考模板写进正文时，要能剥离（2026-11-15 夏瑾线上事故）。

跑法：python tests/test_thinking_strip.py
覆盖：
1. 成对思考块（<think> / <｜begin▁of▁thinking｜>）在任意位置都摘掉；
2. 只有开头没结尾时，用 spec.plain_body.after 的最靠后一次锚点当回答起点；
3. 正常正文不受影响（没有思考标记时原样返回）；
4. 正文全在 reasoning 时，_body_from_reasoning 按 spec 锚点抽正文。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("REWRITE_ROUTER_CONFIG", str(ROOT / "config.example.json"))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from app import pipeline, presets  # noqa: E402

OPEN = "好的，这是你需求的最终输出："


def make_spec(anchors):
    return presets._spec_from_json({
        "id": "test-anchor", "name": "test-anchor",
        "structure": {"plain_body": {"after": list(anchors)}},
    }, where="test")


def test_paired_blocks_stripped():
    spec = make_spec([OPEN])
    text = ("<think>先想一下用户要什么。</think>\n\n正文第一句。正文第二句。\n\n"
            "<｜begin▁of▁thinking｜>再检查一遍<｜end▁of▁thinking｜>")
    leaked, clean = presets.strip_thinking(text, spec)
    clean = clean.strip()
    assert "先想一下" not in clean and "再检查一遍" not in clean, clean
    assert clean.startswith("正文第一句"), clean
    assert "先想一下" in leaked and "再检查一遍" in leaked, leaked


def test_unclosed_uses_last_anchor():
    spec = make_spec([OPEN])
    text = ("<｜begin▁of▁thinking｜>\n" + "思考模板：先分析、再规划、最后写。\n\n"
            + "现在写。" + OPEN + "\n\n正文从这里开始。")
    leaked, clean = presets.strip_thinking(text, spec)
    assert "思考模板" not in clean, clean
    assert "正文从这里开始" in clean, clean
    assert "思考模板" in leaked, leaked


def test_no_marker_unchanged():
    spec = make_spec([OPEN])
    text = "普通的正文，没有任何思考模板。"
    leaked, clean = presets.strip_thinking(text, spec)
    assert leaked == "" and clean == text


def test_body_from_reasoning_uses_anchor():
    spec = make_spec([OPEN])
    reasoning = ("用户要一段剧情。先分析人设、时间、地点，然后输出。\n\n"
                 + OPEN + "\n\n正文第一句。正文第二句。")
    body = pipeline._body_from_reasoning(reasoning, spec)
    assert body.startswith(OPEN), body
    assert "正文第一句" in body and "先分析人设" not in body, body


def main():
    tests = [test_paired_blocks_stripped, test_unclosed_uses_last_anchor,
             test_no_marker_unchanged, test_body_from_reasoning_uses_anchor]
    passed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
            print("PASS", fn.__name__)
        except AssertionError as exc:
            print("FAIL", fn.__name__, "->", exc)
    print("%d/%d passed" % (passed, len(tests)))
    if passed != len(tests):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
