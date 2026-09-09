"""生成「本地数据 → 平台输入 JSONL」的切分脚本。

脚本主体是 templates/split_script.py.tmpl 这个真实的 Python 文件，
生成时只往里注入一段 JSON 配置 —— 用户填的 prompt 里带引号、反斜杠、
三引号都不会破坏脚本语法（配置以 repr() 出来的字符串字面量嵌入）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "split_script.py.tmpl"

# {{变量名}}
VAR_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

DEFAULT_SCRIPT_NAME = "build_batch_input.py"


def extract_variables(template: str) -> list[str]:
    """列出模板里引用到的变量名（去重、保持出现顺序）。"""
    seen: list[str] = []
    for match in VAR_PATTERN.finditer(template or ""):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def validate(config: Any) -> list[str]:
    """返回人能看懂的问题列表；空列表表示没问题。"""
    problems: list[str] = []

    declared = {v.name for v in config.variables}
    used = set(extract_variables(config.prompt_template))

    missing = used - declared
    if missing:
        problems.append(
            f"Prompt 里用到了未定义的变量：{'、'.join(sorted(missing))}。"
            "请在「数据变量」里补上，否则生成的 prompt 会原样带着 {{占位符}}。"
        )
    unused = declared - used
    if unused:
        problems.append(f"定义了但没在 Prompt 里用到的变量：{'、'.join(sorted(unused))}")

    if config.custom_id_mode == "field" and not config.custom_id_field:
        problems.append("custom_id 选择了「读取字段」，但没填字段名")

    if not (config.prompt_template or "").strip():
        problems.append("Prompt 模板为空")

    dupes = [v.name for v in config.variables if [x.name for x in config.variables].count(v.name) > 1]
    if dupes:
        problems.append(f"变量名重复：{'、'.join(sorted(set(dupes)))}")

    return problems


def _config_payload(config: Any) -> dict:
    """转成生成脚本里那份 CONFIG。字段名与模板里的读取一一对应。"""
    return {
        "source_path": config.source_path,
        "source_format": config.source_format,
        "csv_delimiter": config.csv_delimiter,
        "encoding": config.encoding,
        "recursive": config.recursive,
        "custom_id_mode": config.custom_id_mode,
        "custom_id_field": config.custom_id_field,
        "custom_id_prefix": config.custom_id_prefix,
        "variables": [{"name": v.name, "field": v.field} for v in config.variables],
        "system_prompt": config.system_prompt or "",
        "prompt_template": config.prompt_template,
        "model": config.model or "",
        "endpoint_path": config.endpoint_path,
        "extra_body": config.extra_body or {},
        "output_dir": config.output_dir,
        "output_prefix": config.output_prefix,
        "max_rows_per_file": config.max_rows_per_file,
        "max_file_size_mb": config.max_file_size_mb,
        "max_prompt_chars": config.max_prompt_chars,
        "max_line_bytes": config.max_line_bytes,
        "on_oversize": config.on_oversize,
        "skip_empty": config.skip_empty,
        "check_duplicate_ids": config.check_duplicate_ids,
    }


def generate(config: Any, script_name: str = DEFAULT_SCRIPT_NAME) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = json.dumps(_config_payload(config), ensure_ascii=False, indent=2)
    # repr() 出来的一定是合法的 Python 字符串字面量，任何字符都不会破坏语法
    return template.replace("__CONFIG_JSON__", repr(payload)).replace("__SCRIPT_NAME__", script_name)


def preview_record(config: Any, sample_row: dict[str, Any] | None = None) -> dict:
    """按配置渲染一条示例记录，让用户在生成前就能看到输出长什么样。"""
    row = sample_row or {}
    if not row:
        # 没给样例数据时，用变量名占位造一行，至少能看清结构
        row = {v.field: f"<{v.field} 的值>" for v in config.variables}
        if config.custom_id_mode == "field" and config.custom_id_field:
            row[config.custom_id_field] = "<该字段的值>"

    def field_value(field: str) -> str:
        value = row.get(field)
        if value is None:
            return ""
        if isinstance(value, dict | list):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    values = {v.name: field_value(v.field) for v in config.variables}
    prompt = config.prompt_template
    for name, value in values.items():
        prompt = prompt.replace("{{" + name + "}}", value)

    if config.max_prompt_chars and len(prompt) > config.max_prompt_chars:
        prompt = prompt[: config.max_prompt_chars] if config.on_oversize == "truncate" else prompt

    messages = []
    if config.system_prompt:
        messages.append({"role": "system", "content": config.system_prompt})
    messages.append({"role": "user", "content": prompt})

    body: dict[str, Any] = {"messages": messages}
    body.update(config.extra_body or {})
    if config.model:
        body["model"] = config.model

    if config.custom_id_mode == "field":
        custom_id = field_value(config.custom_id_field or "") or "row-1"
    elif config.custom_id_mode == "uuid":
        custom_id = "3f2a1c9e8b7d4f60a1b2c3d4e5f60718"
    else:
        custom_id = "1"

    return {
        "custom_id": f"{config.custom_id_prefix}{custom_id}",
        "method": "POST",
        "url": config.endpoint_path,
        "body": body,
    }
