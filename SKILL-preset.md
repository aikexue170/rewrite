---
name: rewrite-preset-adaptation
description: 给「薯饼路由器 / rewrite」自部署用户适配 SillyTavern 第三方预设：从用户本地预设生成 preset_specs/*.json、人工补齐结构锚点、验证只改正文。Use when the router skips rewrite with "正文被切得太碎（N 段）", says the preset is not recognized, rewrites planning/status bars, or the user installs/switches an ST preset.
---

# 适配预设（给 AI agent 的操作 skill）

> 本文件是给 AI coding agent 的执行说明。用户遇到下面的问题时，先读本文件再动手。

## 0. 什么时候必须做

只要用户用**第三方 ST 预设**（夏瑾、梦鲸思客、泉此方、梁苑、图灵、以及任何非本仓库示例的预设），
就必须在用户本机生成对应的 `preset_specs/<id>.json` 并补齐结构规则。**不适配 = 大概率出错**：

| 症状（用户看到的） | 实际原因 |
|---|---|
| `正文被切得太碎（N 段）` 然后跳过改文 | 没有 spec → 回退通用保护；思考/规划/状态栏把正文切碎 |
| 规划、状态栏、变量块被当成正文改写 | 没认出预设，通用保护猜错结构 |
| 思考模板（CoT）被当正文送去改文 | 同上；新版代码还会先剥离写进正文的思考块 |
| 输出里混入大段"思考/要求"文字 | 同上 |

**重要**：本仓库只带 `beileng-3-0.json`（作者自用）和 `example-spec.json`（教学模板）。
第三方预设的 spec **不随仓库分发**（spec 里的指纹是别人预设提示词的原文片段，公开发布等于转载）。
所以适配是自部署用户的必做步骤，而且 spec 只放在用户本地，**不要提交到公开仓库**。

## 1. 先确认当前状态

1. 看用户仓库/config：`presets.enabled`（默认 true）、`presets.dir`（默认 `preset_specs`）。
2. 看 `preset_specs/` 里有没有用户目标预设的 spec；没有就做。
3. 让用户提供两样材料：
   - **ST 预设 JSON**（酒馆「预设」里的导出文件）——生成 match 指纹用；
   - **一条真实的云端原始回复**（不是改写后的）——判断结构、补锚点用。可以从
     `debug/` 日志、路由日志、或前端「云端输出」里拿；也可以在酒馆发一条消息后从日志截取。
4. 如果实在拿不到预设 JSON：只能用 `match.markers`（正文结构标记）做匹配，准确率会低一截；
   尽量还是让用户导出预设。

## 2. 生成 spec 草稿

```bash
python tools/parse_preset.py 用户的预设.json --out preset_specs/<id>.json
# 批量
python tools/parse_preset.py --presets-dir 用户预设定/ --out-dir preset_specs/
# 校验
python tools/parse_preset.py --check preset_specs/
```

自动生成只负责能推断的部分：标签分类、正则/助手脚本清单、跨预设唯一长句指纹。
**结构规则（尤其 `plain_body` 锚点、`keep_prefixes`）必须人工复核。**

## 3. 人工补齐结构规则（核心步骤）

对照那条真实回复，逐项确认：

| 字段 | 含义 | 怎么填 |
|---|---|---|
| `match.fingerprints` | 入站 messages 里能唯一识别该预设的 20+ 字提示词长句 | 从预设 JSON 里挑跨预设唯一的长句；至少 1 条命中 |
| `match.markers` | 正文里出现即加分的结构标记 | 该预设输出里稳定的标签/开场句（如 `<content>`、固定开场语） |
| `keep_tags` | 整块原样保留的标签（未闭合保护到结尾） | 思考、状态栏、变量块、摘要、选项、HTML 面板等 |
| `wrapper_tags` | 标签保留、内容当正文 | 外层包裹标签（如 `<prologue>`） |
| `body_tags` | 唯一正文标签 | 回复里明确包住正文的标签（如 `<content>`） |
| `root_tags` | 只保留标签 token，内容继续按规则处理 | 根标签（如 `<dream_plot>`） |
| `plain_body.after` | 没有 body 标签时的正文起点锚点（**逐字**） | 正文前的固定开场句/分隔标记，如「好的，这是…」这类确认句；代码取**最靠后**一次出现 |
| `plain_body.before` | 正文结束锚点 | 正文后固定的收尾标记 |
| `keep_prefixes` | 需要原样保留、别送改文的前缀 | 开场句、标题行等 |
| `body_is_whole` | 整条回复就是正文 | 零标签预设 |
| `start_if_no_after` | 一个 after 锚点都没命中时的起点 | 一般 0（别乱设） |
| `fallback_after_keep` | body/锚点都失效时，取第一个 keep 块之后最大空白区 | 特殊变体才开 |

判断方法（从真实回复倒推）：

1. 把真实回复按「保留 / 正文」标出来；
2. 正文前如果有一句固定开场（每次都会出现、逐字稳定的），整句放进 `plain_body.after` 和
   `keep_prefixes`；
3. 正文被标签包住 → `body_tags`；正文外有状态/变量块 → `keep_tags`；
4. 正文完全没有标签、整条就是正文 → `body_is_whole: true`；
5. 开场句可能被模型复制进思考里 → 依赖新版代码的 `strip_thinking`（取最靠后的锚点）；
   旧版代码先升级。

**判断锚点的黄金标准**：拿那条真实回复，用锚点切一遍，看切出来的正文是不是用户想改的那段
（不是思考、不是大纲、不是状态栏、不是选项）。

## 4. 验证（必须做，不要跳过）

```bash
python tools/parse_preset.py --check preset_specs/
# 起服务后发一条真实请求；观察路由日志/前端：
#   - 出现「预设命中：xxx（messages/text，score=…）」
#   - 结构块原样保留，只有正文被改写
#   - 没有「正文被切得太碎」「预设命中但切不出正文区」
```

调试开关：`config.json` 里 `presets.note_in_reasoning: true` 会让输出里显示「已识别预设：xxx」，
确认命中的是哪份 spec。改完 spec 不用重启（目录变了自动重载）。

如果命中但切不出正文：先补 `plain_body` 锚点；如果没命中：检查 `fingerprints`/`markers` 与
`min_fingerprints`/`min_markers`；如果 CoT/规划被当正文：把对应标签加进 `keep_tags`，并确认
代码是 2026-11-15 之后（包含 `strip_thinking` 的版本）。

## 5. 提交前检查

- `preset_specs/<第三方>.json` 只留在用户本地，**绝不提交/公开**（`.gitignore` 已覆盖）。
- 不要把用户预设 JSON、提示词原文、指纹贴进 issue/README/聊天记录。
- 改 spec 不需要改代码；除非是仓库 bug（如缺 `strip_thinking`），那种情况走代码修复。

## 6. 最小示例（generic，不放真实指纹）

```jsonc
{
  "id": "example-preset",
  "name": "示例预设",
  "match": {
    "fingerprints": ["这里放 20 字以上、跨预设唯一的提示词长句"],
    "markers": ["<content>"],
    "min_fingerprints": 1,
    "min_markers": 1,
    "min_text_score": 8
  },
  "structure": {
    "keep_tags": ["think", "UpdateVariable", "details", "StatusBlock"],
    "body_tags": ["content"],
    "plain_body": {
      "after": ["好的，这是你需要的内容："],   // 换成用户预设真实的固定开场句
      "before": []
    },
    "keep_prefixes": ["好的，这是你需要的内容："]
  }
}
```

填完跑 `python tools/parse_preset.py --check preset_specs/`，再用真实回复验证即可。
