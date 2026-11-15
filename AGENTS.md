# AGENTS.md

本仓库的部署说明书在 **[SKILL.md](SKILL.md)**。

如果你是一个 AI coding agent，被要求部署/配置/排查本项目，请先读 `SKILL.md`，按其中的顺序执行：
环境检查 → 装路由依赖 → **适配预设（SKILL-preset.md）** → 下载模型（Hugging Face）→ 填 `config.json` → 起三个服务 → 验证 → 告诉用户接入地址。

> ⚠️ **第一优先级：确认用户的 ST 预设已适配 spec。** 本仓库只带 `beileng-3-0` + `example-spec`，
> 第三方预设（夏瑾/梦鲸/泉此方等）的 spec 不随仓库分发；没适配就会退化为通用保护，典型症状是
> **`正文被切得太碎（N 段）` 跳过改文、规划/状态栏被当正文改、思考模板混进正文**。
> 完整操作见 **[SKILL-preset.md](SKILL-preset.md)**；不要先改代码，先按该 skill 适配预设。

- 项目介绍 / 快速上手：[README.md](README.md)
- 部署细节 / 配置全表 / 原理 / FAQ：[TECHNICAL.md](TECHNICAL.md)

关键约束（务必遵守）：

0. **先适配预设**：第三方预设必须用用户本地预设生成 `preset_specs/*.json` 并人工补结构锚点
   （`plain_body.after` / `keep_tags` 等），流程见 [SKILL-preset.md](SKILL-preset.md)。
   没适配就排查代码是白费力气。
1. `config.json` 含用户云端 key，已在 `.gitignore`，**绝不提交**。
2. 不要修改 `app/sentences.py`（分句口径必须与 jev 训练一致）。
3. 默认不要关闭 `protection`（前端卡结构保护）与 `presets`（预设解析，spec 在 `preset_specs/`）。
4. 新增/修改 `preset_specs/*.json` 后跑 `python tools/parse_preset.py --check preset_specs/`。
5. 未经用户要求，不要把服务暴露到公网（无鉴权）。
6. 第三方预设的 spec、指纹、预设原文只放用户本地，**绝不提交/公开**（`.gitignore` 已覆盖 spec）。
7. 修改预设解析/思考剥离相关代码后，至少跑 `python tests/test_presets.py` 和
   `python tests/test_thinking_strip.py`。
