---
name: batch-results
description: 查看批量推理任务的进度、等待完成、排查失败条目、下载结果，并按 custom_id 把结果合并回原始数据。用户问"任务跑完了吗""把结果下下来""结果合并回表格""重跑失败的"时使用。
argument-hint: "[任务 ID 或清单文件]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bic.py whoami*) Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bic.py jobs*) Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bic.py status*) Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bic.py errors*)
---

# 批量推理：进度、结果与合并

命令行工具（Windows 上把 `python3` 换成 `python`）：

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bic.py <命令> ...
```

下文用 `bic` 代指。连接未配置或返回 401 时，让用户到平台「个人设置 → API Token」新建 token，并在自己的终端执行页面给出的配置命令。

## 找到任务

- batch-submit 写过的清单（`bic_manifest.json`）：命令里用 `--manifest <路径>`。
- 用户直接给了任务 ID：把 ID 作为位置参数传入。
- 都没有：用 `bic jobs` 列出最近的任务（`--status running,queued` 可筛选），让用户挑。

## 查看进度

```bash
bic status --manifest <清单>
```

状态的含义：
- `queued`：排队中，`queue_position` 是排队位置。
- `running`：执行中。
- `paused`：已暂停。
- `succeeded`：全部成功。
- `completed`：已跑完，但有失败条目。
- `failed`：任务级失败，看 `error`。
- `canceled`：已取消。

## 等待完成

大任务可能要跑几个小时，而单条命令最多只能运行 10 分钟。所以**要在后台运行** `wait`（Bash 工具设置 `run_in_background: true`）：

```bash
bic wait --manifest <清单> --interval 60
```

任务全部结束或暂停时，`wait` 会退出，你会收到通知。退出码 0 表示全部为 succeeded/completed，1 表示有 failed、canceled 或 paused。用户只是想看一眼进度时，用 `status` 就够了。

## 排查失败

```bash
bic errors <任务ID> --limit 20
```

先归类失败原因，再给用户建议：
- 429 / 限流、超时：通常重跑即可。
- 400：请求本身有问题，比如超长、参数不被接受，重跑也没用，要回到数据或参数上修正。
- 401 / 403：key 或网关 token 有问题。

需要重跑失败条目时，**先征得用户同意**（会再次消耗额度），再执行：

```bash
bic retry-failed <任务ID>
```

## 更换模型或修改参数

任务处于 `paused`、`canceled` 或 `failed` 时可以换模型、改推理参数，比如原模型被下线、限流太严、`max_tokens` 给小了导致输出被截断。恢复后剩余条目按新配置跑，已完成的条目不会重跑，所以结果里会同时有新旧两份配置的输出。

恢复会继续消耗额度，**先征得用户同意**：

```bash
bic set-model <任务ID> --model <模型> --resume                  # 换模型，参数沿用
bic set-model <任务ID> --max-tokens 4096 --temperature 0.2 --resume   # 只改参数，模型不变
```

- 参数选项和 `submit` 一样（`--system-prompt`、`--temperature`、`--max-tokens`、`--reasoning-effort`、`--extra` 等）。只改给出的那几项，其余沿用原任务的参数。
- 不带 `--resume` 只保存、不入队，之后再用 `bic resume <任务ID>` 恢复。
- 个人网关模型只能换到自己的任务上。
- 已失败的条目不会随恢复重跑；任务结束后用 `retry-failed` 重跑，这时用的也是新配置。

## 下载结果

只有状态为 succeeded、completed 或 canceled 的任务才能下载。

```bash
bic download --manifest <清单> -o ./batch_results
```

- 默认格式是 `simple`：每行一个 `custom_id` + `output` + `error` + `usage`，包含失败条目。
- `--format raw`：原始完整响应。
- `--format csv`：适合直接用 Excel 打开。
- 报 410：结果已超过保留期被平台清理，无法再下载。
- 结果按完成顺序写入，不是原始顺序，要恢复顺序请用下面的 join。

## 合并回原始数据

数据是用 batch-prep 生成的：用它输出目录里的 `bic_prep_config.json`，数据源和 custom_id 规则会自动带出。

```bash
bic join --config <输出目录>/bic_prep_config.json --results ./batch_results/*.jsonl -o merged.csv
```

没有 prep 配置：手动指定数据源和 custom_id 规则。

```bash
bic join --source <原始数据> --id-mode field --id-field <列名> --results <结果...> -o merged.csv
```

输出会在原有列之后追加这几列：
- `result_status`：`ok` / `error` / `missing`
- `result_output`
- `result_error`
- `result_prompt_tokens`
- `result_completion_tokens`

`-o` 用 `.jsonl` 后缀时，输出为 JSONL。

汇报 `ok` / `error` / `missing` 的数量。`missing` 通常是预处理时被跳过的行，比如空 prompt 或超长文本。如果 `missing` 很多，或 `results_unmatched > 0`，说明 custom_id 规则对不上（例如 prefix 不一致，或源文件在预处理后被改动过），要告诉用户。

不要把合并结果整个打印出来：需要展示时只看几行，大量数据的统计用脚本来算。
