"""输入 JSONL 的解析与校验。

兼容三种写法（由宽到严）：
1. OpenAI Batch 风格：{"custom_id": "a", "method": "POST", "url": "/v1/chat/completions",
                       "body": {"model": "...", "messages": [...]}}
2. 精简 messages：    {"custom_id": "a", "messages": [{"role": "user", "content": "..."}]}
3. 纯 prompt：        {"custom_id": "a", "prompt": "你好"}  /  {"prompt": "你好"}

统一归一化为 {"custom_id": str, "body": {...}}，body 为 OpenAI /chat/completions 请求体。
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_PREVIEW_ERRORS = 20


class JsonlError(ValueError):
    """输入文件不合法。"""


@dataclass
class ParsedItem:
    index: int
    custom_id: str
    body: dict[str, Any]


@dataclass
class ValidationResult:
    total: int
    errors: list[str]
    preview: list[dict[str, Any]]


def normalize_line(raw: str, index: int) -> ParsedItem:
    """把一行 JSONL 归一化成 ParsedItem，失败抛 JsonlError。"""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JsonlError(f"第 {index + 1} 行不是合法 JSON: {exc.msg}") from exc

    if not isinstance(obj, dict):
        raise JsonlError(f"第 {index + 1} 行必须是 JSON 对象")

    custom_id = str(obj.get("custom_id") or obj.get("id") or f"request-{index + 1}")

    body = obj.get("body")
    if isinstance(body, dict):
        pass
    elif "messages" in obj:
        body = {k: v for k, v in obj.items() if k not in {"custom_id", "id", "method", "url"}}
    elif "prompt" in obj:
        prompt = obj["prompt"]
        if not isinstance(prompt, str):
            raise JsonlError(f"第 {index + 1} 行的 prompt 必须是字符串")
        body = {k: v for k, v in obj.items() if k not in {"custom_id", "id", "method", "url", "prompt"}}
        body["messages"] = [{"role": "user", "content": prompt}]
    else:
        raise JsonlError(f"第 {index + 1} 行缺少 body / messages / prompt 字段")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise JsonlError(f"第 {index + 1} 行的 messages 为空或不是数组")
    for m in messages:
        if not isinstance(m, dict) or "role" not in m or "content" not in m:
            raise JsonlError(f"第 {index + 1} 行的 messages 元素必须含 role 与 content")

    return ParsedItem(index=index, custom_id=custom_id, body=body)


def iter_items(path: Path) -> Iterator[ParsedItem]:
    """流式读取整个文件；空行自动跳过（不占用 index）。"""
    index = 0
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            yield normalize_line(raw, index)
            index += 1


def validate_file(path: Path, max_items: int, preview_size: int = 3) -> ValidationResult:
    """一次全量扫描：统计条数、收集前若干条错误与预览。"""
    total = 0
    errors: list[str] = []
    preview: list[dict[str, Any]] = []

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = normalize_line(raw, total)
                if len(preview) < preview_size:
                    preview.append({"custom_id": item.custom_id, "body": item.body})
            except JsonlError as exc:
                if len(errors) < MAX_PREVIEW_ERRORS:
                    errors.append(str(exc))
            total += 1
            if total > max_items:
                errors.append(f"条目数超出上限 {max_items}")
                break

    if total == 0:
        errors.insert(0, "文件为空或不含任何有效行")

    return ValidationResult(total=total, errors=errors, preview=preview)


def count_duplicate_custom_ids(path: Path, limit: int = 10) -> list[str]:
    """检查 custom_id 是否重复——重复会让结果难以对齐回原始数据。"""
    seen: set[str] = set()
    dupes: list[str] = []
    for item in iter_items(path):
        if item.custom_id in seen:
            if len(dupes) < limit:
                dupes.append(item.custom_id)
        else:
            seen.add(item.custom_id)
    return dupes
