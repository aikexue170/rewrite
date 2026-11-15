#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预设解析自测：spec 加载 / 匹配 / 结构抽取 / 流水线只改正文。

跑法（不需要 pytest）：

    python tests/test_presets.py

注意：仓库只内置 `beileng-3-0.json`（作者自用）和 `example-spec.json`（教学模板），
第三方预设 spec 不随仓库分发；这里用示例 spec 覆盖通用机制。

覆盖：
1. preset_specs/ 能加载（内置两份）；
2. 正文标记能匹配示例预设，未命中返回 None；
3. 示例结构：<example_think> 原样保留，只有 <example_body> 内容送改文；
4. 零标签预设（北棱 3.0）：messages 指纹匹配 + 整条消息当正文，StatusBlock 原样保留；
5. 截断输出（keep 标签未闭合）绝不能把规划当正文；
6. parts 覆盖原文全部分字符（重组不丢字）；
7. 流水线集成：命中预设时结构块原样流出、正文被改写、stats["preset"] 有记录。
"""
import os
import sys
import asyncio
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

EXAMPLE_ID = "example-preset"
BEILENG_ID = "beileng-3-0"
BEILENG_FP = "你还可以发现，在整个文段中我尽量保持着修辞手法的来回变动，不让同一个句式反复出现"


def _prose() -> str:
    return "她把伞收起来，抖了抖水，抬头看了一眼天色，然后慢慢走进了楼道里。" * 3


def _example_output() -> str:
    return (
        "<example_think>这是规划，不能拿去改写。先想清楚节奏和落点。</example_think>\n"
        f"<example_body>{_prose()}</example_body>"
    )


def test_specs_loaded() -> None:
    presets.clear_cache()
    specs = presets.load_specs()
    assert EXAMPLE_ID in specs, "缺少 example-spec.json"
    assert BEILENG_ID in specs, "缺少 beileng-3-0.json"
    for spec_id, spec in specs.items():
        assert spec.id == spec_id, f"spec id 与预期不一致：{spec_id}"


def test_match_by_marker() -> None:
    specs = presets.load_specs()
    match = presets.match_text("<example_body>正文正文正文正文正文正文</example_body>", specs)
    assert match is not None and match.spec.id == EXAMPLE_ID, "正文标记没匹配上示例预设"
    assert presets.match_text("一段没有任何预设标记的普通小说正文。" * 5, specs) is None, \
        "无标记文本不应命中"


def test_plan_keeps_structure_and_prose_region() -> None:
    text = _example_output()
    spec = presets.load_specs()[EXAMPLE_ID]
    plan = presets.plan_reply(
        text, spec,
        protection_cfg={"min_prose_ratio": 0.15, "max_segments": 4,
                        "min_rewrite_chars": 60},
        min_rewrite_chars=60)
    assert plan is not None and not plan.skipped, "示例输出应能切出正文"
    assert "".join(part.text for part in plan.parts) == text, "重组后丢字了"
    prose = "".join(part.text for part in plan.parts if part.kind == "prose").strip()
    assert "她把伞收起来" in prose
    assert "这是规划" not in prose


def test_zero_tag_preset_match_and_whole_body() -> None:
    text = ("<StatusBlock>好感度 12 / 时间 22:40</StatusBlock>\n" + _prose())
    specs = presets.load_specs()
    match = presets.match_messages(
        [{"role": "system", "content": f"预设提示词……{BEILENG_FP}……"}], specs)
    assert match is not None and match.spec.id == BEILENG_ID, "messages 指纹没匹配上北棱预设"
    spec = specs[BEILENG_ID]
    plan = presets.plan_reply(text, spec, min_rewrite_chars=60)
    assert plan is not None and not plan.skipped, "零标签预设应整条当正文"
    assert "".join(part.text for part in plan.parts) == text
    prose = "".join(part.text for part in plan.parts if part.kind == "prose")
    assert "她把伞收起来" in prose and "StatusBlock" not in prose


def test_truncated_output_never_rewrites_plan() -> None:
    text = "<example_think>这是规划，不能改写。"       # 未闭合的 keep 标签
    spec = presets.load_specs()[EXAMPLE_ID]
    plan = presets.plan_reply(text, spec, min_rewrite_chars=60)
    assert plan is not None and plan.skipped, "截断输出不能把规划当正文改写"


def test_pipeline_only_rewrites_body() -> None:
    text = _example_output()
    match = presets.match(text=text, specs=presets.load_specs())
    assert match is not None

    events: list[tuple[str, str]] = []

    async def emit(kind: str, payload: str) -> None:
        events.append((kind, payload))

    async def fake_scores(units):
        return [0.2 for _ in units]

    async def fake_rewrite(marked_text, on_reasoning, on_content):
        await on_reasoning("改文思考")
        await on_content("改写后的正文")
        return "改写后的正文"

    orig_scores = pipeline.jev.score_units
    orig_rewrite = pipeline.rewrite.stream_rewrite
    pipeline.jev.score_units = fake_scores
    pipeline.rewrite.stream_rewrite = fake_rewrite
    try:
        stats = asyncio.run(pipeline._process_body(text, False, emit,
                                                   preset_match=match))
    finally:
        pipeline.jev.score_units = orig_scores
        pipeline.rewrite.stream_rewrite = orig_rewrite

    content = "".join(payload for kind, payload in events if kind == "content")
    assert "<example_think>这是规划" in content, "规划块被改掉了"
    assert "她把伞收起来" not in content, "正文没有被改写"
    assert "改写后的正文" in content, "没看到改写结果"
    assert stats["preset"]["id"] == EXAMPLE_ID, "stats 里没记录预设"


def main() -> int:
    tests = [
        test_specs_loaded,
        test_match_by_marker,
        test_plan_keeps_structure_and_prose_region,
        test_zero_tag_preset_match_and_whole_body,
        test_truncated_output_never_rewrites_plan,
        test_pipeline_only_rewrites_body,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        finally:
            presets.clear_cache()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
