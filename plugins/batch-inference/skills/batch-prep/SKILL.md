---
name: batch-prep
description: 把本地的 CSV / JSONL / Parquet 数据预处理成批量推理平台可上传的 JSONL —— 套用 Prompt 模板、生成 custom_id、按行数和大小切分。用户想"准备批量推理数据""把这个表格转成推理输入""用某个 Prompt 批量处理本地文件"时使用。
argument-hint: "[数据文件或目录]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bic.py whoami*) Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bic.py models*) Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bic.py prompts*) Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bic.py inspect*)
---

# 批量推理：本地数据预处理

目标：把用户的本地数据变成平台能直接上传的 JSONL 分片。**数据只在本机处理**，发给平台的只有切分配置（模板、列名、路径），平台据此生成脚本，脚本在本机运行。

所有操作都通过命令行工具完成（Windows 上把 `python3` 换成 `python`）：

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bic.py <命令> ...
```

下文用 `bic` 代指这条命令。结果以 JSON 输出到 stdout，过程信息在 stderr。

## 数据隐私（必须遵守）

- `inspect` 先用 `--schema-only`：只看列名、类型、空值率，不看任何数据值。
- 需要看样例才能判断字段含义时，**先征得用户同意**再去掉 `--schema-only`（样例会进入对话上下文）。用户说数据敏感就只用列名工作。
- 不要用 `cat` / `head` 读取整个数据文件或产出的 JSONL。

## 步骤

### 1. 确认已连接平台

```bash
bic whoami
```

报"还没有配置"或 401 时停下来，告诉用户：
1. 打开平台「个人设置 → API Token」新建一个 token；
2. 在**自己的终端**里执行页面上给出的配置命令（会写入 `~/.config/bic/config.json`）。

**不要让用户把 token 粘贴到对话里**，也不要替用户执行包含 token 的命令。

`whoami` 输出里的 `limits` 是平台的上传上限，下面切分时要用。

### 2. 了解数据

```bash
bic inspect <路径> --schema-only
```

目录会按扩展名自动识别格式。注意输出里的：
- `encoding`：带 BOM 的是 `utf-8-sig`，中文 Windows 导出的常是 `gb18030`。配置里要照填，否则会乱码或第一列列名多出不可见字符。
- `csv_delimiter`：自动探测到的分隔符。
- `hints`：可以作为 custom_id 的列。
- `rows`：总行数，用来估算分片数。

### 3. 确定 Prompt

先看用户有没有保存过合适的模板：

```bash
bic prompts            # 列表
bic prompts <名称>     # 完整内容
```

没有就和用户一起写。模板里用 `{{变量名}}` 引用数据，每个变量要映射到一列：`{"name": "text", "field": "正文"}`。system prompt 可选。

### 4. 选 custom_id

custom_id 用来把结果对回原始行，**必须能对回去**：
- 有唯一 ID 列 → `"custom_id_mode": "field"`，`"custom_id_field": "<列名>"`（首选）。
- 没有 → `"custom_id_mode": "rownum"`（按读取顺序编号，源文件之后不能再改动顺序）。
- 不要用 `uuid`：结果将无法合并回原始数据。

### 5. 写配置文件

在数据旁边写一个 JSON 配置（如 `<数据目录>/bic_prep.json`），字段与平台「脚本生成」页面一致：

```json
{
  "source_path": "./data/reviews.csv",
  "source_format": "csv",
  "encoding": "utf-8",
  "csv_delimiter": ",",
  "recursive": false,
  "custom_id_mode": "field",
  "custom_id_field": "review_id",
  "custom_id_prefix": "",
  "variables": [{"name": "text", "field": "content"}],
  "system_prompt": "你是一个情感分析助手。",
  "prompt_template": "判断下面评论的情感倾向，只回答 正面/负面/中性：\n\n{{text}}",
  "output_dir": "./batch_input",
  "output_prefix": "part",
  "max_rows_per_file": 50000,
  "max_file_size_mb": 100,
  "max_prompt_chars": 0,
  "max_line_bytes": 0,
  "on_oversize": "skip",
  "skip_empty": true,
  "check_duplicate_ids": true
}
```

要点：
- 用了保存的 Prompt 时，可以省略 `prompt_template` / `system_prompt` / `variables`，运行时加 `--prompt <名称>`；但如果新数据的列名和当初不同，要在配置里写上 `variables` 覆盖。
- `max_file_size_mb` 必须小于 `whoami` 里的 `limits.max_upload_mb`，`max_rows_per_file` 必须不超过 `limits.max_items_per_job`。每个分片最终会成为一个任务。
- `model` 留空即可，模型在提交任务时再选。
- `max_prompt_chars` 用来防止超长文本撑爆上下文；`on_oversize` 选 `skip`（跳过）或 `truncate`（截断）。

### 6. 试跑，再正式跑

```bash
bic prep --config <配置> --limit 20        # 只处理前 20 行
```

- `ok: false` 且 `stage: config`：配置有问题（如模板里的变量没定义），按 `problems` 修正后重跑。只有确认问题可以忽略时才加 `--force`。
- `sample_line` 是用占位符渲染的请求结构（不含真实数据），用来确认 system / user 消息拼得对不对。想看真实渲染结果，征得用户同意后再读产出文件的第一行（`head -n 1 <分片>`）。
- stderr 里有跳过明细（空 prompt、超长），跳过比例异常时告诉用户。

确认无误后正式生成：

```bash
bic prep --config <配置>
```

如果报输出目录里已有旧分片，**先问用户**能否删除，再加 `--overwrite`。

### 7. 汇报

告诉用户：产出了几个分片、各多少行、总行数、跳过了多少行及原因，以及 `bic_prep_config.json` 的位置（之后合并结果要用）。然后建议用 **batch-submit** 上传并提交任务。
