"""输入 JSONL 的三种写法与错误提示。"""
from __future__ import annotations

import json

import pytest

from app.services.jsonl import JsonlError, count_duplicate_custom_ids, normalize_line, validate_file


def test_openai_batch_format():
    raw = json.dumps({
        "custom_id": "a1", "method": "POST", "url": "/v1/chat/completions",
        "body": {"model": "ignored", "messages": [{"role": "user", "content": "hi"}]},
    })
    item = normalize_line(raw, 0)
    assert item.custom_id == "a1"
    assert item.body["messages"][0]["content"] == "hi"


def test_plain_messages_format():
    raw = json.dumps({"custom_id": "b", "messages": [{"role": "user", "content": "x"}], "temperature": 0.1})
    item = normalize_line(raw, 0)
    assert item.body["temperature"] == 0.1


def test_prompt_shorthand_becomes_user_message():
    item = normalize_line(json.dumps({"prompt": "写首诗"}), 4)
    assert item.custom_id == "request-5"  # 缺省 custom_id 用 1-based 行号
    assert item.body["messages"] == [{"role": "user", "content": "写首诗"}]


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps(["list", "not", "object"]),
        json.dumps({"custom_id": "x"}),                       # 三种字段一个都没有
        json.dumps({"messages": []}),                          # 空 messages
        json.dumps({"messages": [{"role": "user"}]}),          # 缺 content
    ],
)
def test_bad_lines_rejected(raw):
    with pytest.raises(JsonlError):
        normalize_line(raw, 0)


def test_validate_file_counts_and_reports(tmp_path):
    p = tmp_path / "in.jsonl"
    p.write_text(
        "\n".join([
            json.dumps({"prompt": "a"}),
            "",                                   # 空行会被跳过，不计入条目
            json.dumps({"prompt": "b"}),
            "{oops",
        ]),
        encoding="utf-8",
    )
    result = validate_file(p, max_items=100)
    assert result.total == 3
    assert len(result.errors) == 1
    assert "第 3 行" in result.errors[0]
    assert len(result.preview) == 2


def test_validate_empty_file(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("\n\n", encoding="utf-8")
    assert validate_file(p, 100).errors[0].startswith("文件为空")


def test_duplicate_custom_ids_detected(tmp_path):
    p = tmp_path / "dupe.jsonl"
    p.write_text("\n".join(json.dumps({"custom_id": "same", "prompt": str(i)}) for i in range(3)),
                 encoding="utf-8")
    assert count_duplicate_custom_ids(p) == ["same", "same"]
