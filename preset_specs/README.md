# preset_specs/ · 预设解析 spec 仓库

每份 ST（SillyTavern）Chat Completion 预设一个 JSON。路由器后端（`app/presets.py`）
启动时/请求时读这个目录（目录内容变了自动重载，改完不用重启），用它们：

1. **匹配**用户当前在用的预设（入站 messages 的提示词指纹 / 正文结构标记）；
2. **抽取**要改写的正文（结构块原样保留），再交给 `protection` + JEV + 改文流水线。

## 本目录只带两份 spec

- `beileng-3-0.json`：作者自用预设（零标签，整条消息即正文），可直接用；
- `example-spec.json`：**教学模板**，复制后改成你自己的预设。

梦鲸思客、梁元、图灵、夏瑾、泉此方这类**第三方预设的 spec 不随仓库分发**：
spec 里的指纹是预设提示词原文的片段，结构规则也是照着别人的预设逆向出来的，
公开发布等于转载他人预设内容。正确做法是拿**你自己本地的预设文件**生成一份 spec，
只自己用（本目录的第三方 spec 已在 `.gitignore` 里，不会误提交）：

```bash
python tools/parse_preset.py 你的预设定/某预设.json --out preset_specs/某预设.json
```

## 加新预设（本地）

一行命令生成草稿，再人工补结构规则：

```bash
python3 tools/parse_preset.py 预设定/某预设.json --out preset_specs/某预设.json
# 批量（只扫你自己本地的预设目录）
python3 tools/parse_preset.py --presets-dir 你的预设定/ --out-dir preset_specs/
# 校验
python3 tools/parse_preset.py --check preset_specs/
```

自动生成只负责「能推的」：标签分类、正则脚本清单、酒馆助手脚本清单、跨预设唯一长句。
**结构规则（`plain_body` 锚点、`keep_prefixes`、`body_is_whole`）需要人工复核**
（看模型原始输出长什么样、正则会不会把标签物理删掉）。

## Schema

运行时只读下面这些字段（`regex_scripts` / `tavern_helper_scripts` / `notes` /
`active_prompts` 之类是生成工具早期的审计字段，运行时不用，**也不要写进公开 spec**）：

```jsonc
{
  "id": "example-preset",                    // 唯一 id，文件名建议同名
  "name": "示例预设（展示用）",               // 展示名
  "match": {
    "fingerprints": ["≥20 字的独特提示词长句"],   // 入站 messages 指纹（ST 托管路径）
    "markers": ["<example_body>", "<example_think>"], // 正文里出现即加分（网页粘贴路径）
    "min_fingerprints": 1,                   // 至少命中几条指纹
    "min_markers": 1,                        // 至少命中几个标记
    "min_text_score": 8                      // 标记通道最低分（按标记长度累加）
  },
  "structure": {
    "keep_tags": ["example_think", "StatusBlock"], // 整块原样保留（未闭合保护到结尾）
    "wrapper_tags": ["example_plot"],        // 标签保留、内容当正文
    "body_tags": ["example_body"],           // 唯一正文标签
    "root_tags": [],                         // 只保留标签 token，内容继续按规则处理
    "plain_body": {                          // 没有 body 标签时（保存态/显示态）的锚点
      "after": ["</example_scene>"],         // 取最后一个 after 之后
      "before": ["<example_after>"]          // 其后的第一个 before 之前
    },
    "keep_prefixes": ["好的，这是你要的正文："],  // 固定前缀句整句保留
    "body_is_whole": false                   // true = 整条消息都是正文（零标签预设）
  }
}
```

完整可跑模板见 `example-spec.json`；`beileng-3-0.json` 是零标签 + 指纹匹配的实例。

### 字段语义（抽取时）

- 扫描 `keep_tags ∪ body_tags ∪ wrapper_tags` 的标签 token；`keep_tags` 按配对整块
  保护（**未闭合保护到结尾**，绝不把规划/状态栏当正文）。
- `body_tags` / `wrapper_tags`：标签本身保留，内部内容当正文送去改写；
  内部再交给通用 `protection` 做二次切分（HTML 面板、变量块、占位符仍然原样保留）。
- 没有 body 标签时才用 `plain_body` 锚点（保存态正则会把标签物理删掉）。
- `keep_prefixes`：文本开头（跳过空白）命中即整句保留。
- `body_is_whole`：整条消息当正文（北棱这类零标签预设）。
- 找不到正文区时的行为：正文里有该预设的结构 ⇒ **整条透传不改**（保守）；
  完全没有该预设结构 ⇒ 回退到通用保护层（等价旧行为）。

### 写 spec 前必查

1. 模型**原始输出**骨架（标签顺序、哪个是正文）——看预设里「输出格式」条目；
2. 预设的 `extensions.regex_scripts`：`prompt_only` 只改发给模型的历史，
   `markdown_only` 只改显示，两者都 false 的会在**保存消息时物理删改标签**
   （决定 saved 态长什么样）；
3. 预设正则是否真的在跑：ST 有 `extension_settings.preset_allowed_regex.openai`
   白名单，没进白名单的预设正则完全不执行；
4. 提示词指纹要从**默认开启、用户一般不会关**的条目里挑，避开宏和角色卡内容。

## 安全边界（重要）

- **不要把别人的预设 spec 提交到公开仓库**：指纹是预设提示词原文片段，结构规则是逆向
  别人的预设得到的；公开 = 转载。仓库 `.gitignore` 已忽略本目录除内置两份外的所有
  `*.json`，`tools/parse_preset.py` 生成的文件默认不会进 git。
- 发布 spec 前先删掉 `regex_scripts` / `tavern_helper_scripts` / `notes` /
  `active_prompts` 等非运行时字段，只保留本页 Schema 里的匹配 + 结构字段。
- 指纹挑「默认开启、用户一般不会关」的条目，避开宏、角色卡内容和含用户名的句子。
