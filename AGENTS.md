# AGENTS.md

本仓库的部署说明书在 **[SKILL.md](SKILL.md)**。

如果你是一个 AI coding agent，被要求部署/配置/排查本项目，请先读 `SKILL.md`，按其中的顺序执行：
环境检查 → 装路由依赖 → 下载模型（Hugging Face）→ 填 `config.json` → 起三个服务 → 验证 → 告诉用户接入地址。

- 项目介绍 / 快速上手：[README.md](README.md)
- 部署细节 / 配置全表 / 原理 / FAQ：[TECHNICAL.md](TECHNICAL.md)

关键约束（务必遵守）：

1. `config.json` 含用户云端 key，已在 `.gitignore`，**绝不提交**。
2. 不要修改 `app/sentences.py`（分句口径必须与 jev 训练一致）。
3. 默认不要关闭 `protection`（前端卡结构保护）。
4. 未经用户要求，不要把服务暴露到公网（无鉴权）。
