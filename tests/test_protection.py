#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""结构保护器单测：重点验证「正文包裹词不认识」的各种情况。

跑法： .venv/bin/python tests/test_protection.py
"""

import sys
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.protection import plan_reply  # noqa: E402

PROSE = "雨点砸在窗玻璃上，发出细密的声响。她抬起头，看向门口那个人。" * 6
DIALOG = "“你怎么来了？”她问。\n\n“路过。”他说。" * 5
BODY = PROSE + DIALOG


def kinds(plan) -> str:
    return "+".join("改" if p.kind == "prose" else "留" for p in plan.parts)


def expect_wrapper(tag_open: str, tag_close: str) -> None:
    """不认识的包裹词：标签保留、里面正文照改。"""

    plan = plan_reply(f"{tag_open}\n{BODY}\n{tag_close}")
    assert not plan.skipped, f"{tag_open} 被判定整条跳过"
    assert kinds(plan) == "留+改+留", f"{tag_open} 切分不对：{kinds(plan)}"
    assert plan.parts[0].text == tag_open
    assert plan.parts[-1].text == tag_close
    assert plan.parts[1].text.strip() == BODY


def expect_protect(name: str, block: str) -> None:
    """不认识的 UI 块：整块保留，不能拿去改。"""
    plan = plan_reply(f"{block}\n\n{BODY}")
    assert not plan.skipped
    assert block in "".join(p.text for p in plan.parts if p.kind == "keep"), \
        f"{name} 块没有被完整保留"


def main() -> None:
    # 1) 不认识的包裹词：全部应该「壳留、里面改」
    for open_tag, close_tag in [
        ("<推文>", "</推文>"),
        ("<小说>", "</小说>"),
        ("<小说片段>", "</小说片段>"),
        ("<prose>", "</prose>"),
        ("<正文输出>", "</正文输出>"),
        ("<正文>", "</正文>"),
        ("<content>", "</content>"),
        ("<roleplay_reply>", "</roleplay_reply>"),
        ("<output>", "</output>"),
        ("<writing>", "</writing>"),
    ]:
        expect_wrapper(open_tag, close_tag)
    print("① 不认识的包裹词 10 种：壳保留、里面照改 ✅")

    # 2) 不认识的 UI 块：整块保留
    expect_protect("my_panel", "<my_panel>\n状态：很好\nHP：12\n时间：22:41\n</my_panel>")
    expect_protect("estado", "<estado>\n[level|Lv.1]\n[time|第1天]\n[金币|0]\n</estado>")
    expect_protect("ch_stats", "<ch_stats>\n【名字】张三\n【好感】35%\n【欲望】12%\n</ch_stats>")
    print("② 不认识的 UI 块 3 种：整块保留 ✅")

    # 3) 已知结构块 + 正文：只改正文
    plan = plan_reply(f"{BODY}\n\n<StatusBlock>\n<status>\n时间：22:41\n好感度：12\n</status>\n</StatusBlock>")
    assert not plan.skipped
    assert plan.parts[0].kind == "prose"
    assert all(p.kind == "keep" for p in plan.parts[1:])
    print("③ 已知状态块：只改前面正文，状态栏原样 ✅")

    # 4) 未闭合标签：不能吞掉后面正文
    plan = plan_reply(f"<正文输出>{BODY}")
    assert not plan.skipped and "改" in kinds(plan), "未闭合包裹标记把正文吞了"
    plan = plan_reply(f"<漫游>：【图片】\n{BODY}\n<漫游>：在吗？")
    assert not plan.skipped and "改" in kinds(plan), "未闭合聊天标记把正文吞了"
    print("④ 未闭合标记：不吞正文 ✅")

    # 5) 未闭合的 JSON 变量块：保护到结尾，不能改
    plan = plan_reply(f"{BODY}\n<json_patch>\n[ {{'op':'replace','path':'/hp','value':1}} ]")
    assert plan.parts[-1].kind == "keep" and '"op"' in plan.parts[-1].text or "op" in plan.parts[-1].text
    print("⑤ 未闭合变量块：保护到结尾 ✅")

    # 6) 整块前端面板：整条跳过
    html = ('<div class="ui">\n<style>.ui{color:#fff}</style>\n<p>面板</p>\n</div>\n'
            '<UpdateVariable>\n{"op":"delta","path":"/hp","value":-5}\n</UpdateVariable>')
    plan = plan_reply(html)
    assert plan.skipped, f"整块前端没有跳过：{kinds(plan)}"
    print("⑥ 整块前端面板：整条跳过 ✅")

    # 6b) 开头的状态行：保留，且排在正文之前
    plan = plan_reply("[2024|3|1|周五|22:41|便利店门口]\n[当前状态:良好|学力:B]\n\n" + BODY)
    assert not plan.skipped
    assert plan.parts[0].kind == "keep" and "2024|3|1" in plan.parts[0].text
    assert plan.parts[1].kind == "prose"
    print("⑥b 开头状态栏：保留且排在正文前 ✅")

    # 6c) 开头状态栏 + 正文 + 结尾选项：只切出 1 段正文
    card_style = ("[2023|9|1|五|7:12|高一|早读前|南门]\n"
                  "[当前状态:良好|学力:B|情商:B|颜值:A]\n\n"
                  + BODY +
                  "\n\n结尾选项：\nA. 跟着赵鹏去宿舍放东西\nB. 先去教室报到\nC. 在分班榜前再看一眼")
    plan = plan_reply(card_style)
    got = [p.kind for p in plan.parts]
    assert not plan.skipped and got == ["keep", "prose", "keep"], f"切分不对：{got}"
    assert plan.parts[0].text.startswith("[2023|9|1|五|7:12|高一|早读前|南门]")
    assert plan.parts[-1].text.strip().startswith("结尾选项")
    assert plan.parts[1].text.strip() == BODY
    print("⑥c 开头状态栏 + 正文 + 结尾选项：只切 1 段正文 ✅")

    # 7) 纯正文：一段全改
    plan = plan_reply(BODY)
    assert not plan.skipped and kinds(plan) == "改"
    print("⑦ 纯正文：一段全改 ✅")

    # 8) 行内标签/宏所在的段落保留，其余照改
    plan = plan_reply(f"{PROSE}\n\n{{{{user}}}}上前一步，{DIALOG}\n\n{PROSE}")
    assert not plan.skipped and "改" in kinds(plan) and "留" in kinds(plan)
    print("⑧ 宏/行内标签段落：只保那一段 ✅")

    print("\nPROTECTION TEST PASS ✅")


if __name__ == "__main__":
    main()
