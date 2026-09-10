---
name: batch-submit
description: 把本地 JSONL 上传到批量推理平台并创建推理任务 —— 逐行校验、选择模型和参数、先真实试跑一条、经用户确认后才提交。用户想"上传数据跑批量推理""提交推理任务""用某个模型跑这批数据"时使用。
argument-hint: "[JSONL 文件或目录]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bic.py whoami*) Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bic.py models*) Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bic.py jobs*) Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bic.py status*)
---

# 批量推理：上传并提交任务

命令行工具（Windows 上把 `python3` 换成 `python`）：

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bic.py <命令> ...
```

下文用 `bic` 代指。结果是 stdout 上的 JSON。

**提交任务会真实消耗模型额度。** 不经用户明确确认，绝不带 `--yes` 运行 `submit`。

## 步骤

### 1. 确认连接

```bash
bic whoami
```

未配置或 401：让用户到平台「个人设置 → API Token」新建 token，并在**自己的终端**执行页面给出的配置命令。不要让用户把 token 贴进对话。

### 2. 找到输入文件

- 刚用 batch-prep 生成过：用它的输出目录（默认 `./batch_input`，其中的 `part_*.jsonl`）。
- 用户自带的 JSONL：每行需要是以下之一：
  - OpenAI Batch 格式：`{"custom_id": ..., "body": {"messages": [...]}}`
  - `{"custom_id": ..., "messages": [...]}`
  - `{"custom_id": ..., "prompt": "..."}`

  不符合的话先用 batch-prep 转换。

### 3. 上传

```bash
bic upload <目录或文件...>
```

平台会逐行校验。输出里要看的字段：
- `errors`：有错就停下，把行号和原因告诉用户，修正后重新上传。有错误的文件不允许提交。
- `duplicate_custom_ids`：重复的 ID 会让结果对不回原始数据，要提醒用户。
- `manifest`：清单文件，记录了 upload_id，后面的命令都用 `--manifest` 引用它。

上传的文件暂存在平台上，占用用户的存储配额；一直不提交的话要告诉用户。

### 4. 选模型和参数

```bash
bic models
```

- `shared` 是平台配置的模型；`personal` 是用户个人网关 token 能用的模型。如果 `personal_error` 非空，说明网关 token 有问题，让用户去网页上更新。
- 用户没指定模型，或有多个可能时，列出候选让用户选。
- 推理/思考：模型的 `reasoning_mode` 为 `optional` 时可开启，档位只能取 `reasoning_effort_options` 里的值。
- 常用参数：`--temperature`、`--max-tokens`、`--json-mode`（需模型支持）、`--reasoning-effort <档位>`、`--extra '<JSON>'`。数据里已经带了 system 消息，就不要再加 `--system-prompt`。

### 5. 试跑一条

```bash
bic dry-run --manifest <清单> --model <模型> [参数...]
```

这会真实发送清单里第一个文件的第一条请求（约一条的费用），不会创建任务。
- `ok: true`：把 `content` 摘要和 `usage` 给用户看，确认输出符合预期。第一条不具代表性时，可以加 `--item-index N` 换一条。
- `ok: false`：根据 `error` 和 `response` 排查，常见原因有模型名不对、参数不被接受、token 过期。修正后再试跑，不要直接提交。

### 6. 确认后提交

先不带 `--yes` 生成提交计划：

```bash
bic submit --manifest <清单> --model <模型> --name "<任务名>" [同样的参数...]
```

把计划（模型、参数、任务数、总条数）交给用户确认。**用户明确同意后**再加 `--yes` 执行：

```bash
bic submit --manifest <清单> --model <模型> --name "<任务名>" [同样的参数...] --yes
```

- 多个分片会各自成为一个任务，名字依次是 `任务名 [1/N]`、`任务名 [2/N]`……
- 中途失败（常见原因是超出"同时进行中的任务数"上限，HTTP 429）时，已创建的任务不受影响，并已写入清单。等前面的任务跑完后，只提交剩下的分片：用 `--upload-id` 逐个指定。
- 参数必须和试跑时完全一致。

### 7. 汇报

列出任务 ID、名称、条数和排队位置，并告诉用户：清单文件里记录了这些任务；可以在网页上看进度，也可以让我用 **batch-results** 等待任务完成并取回结果。
