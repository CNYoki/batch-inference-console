"""结果落盘、断点续跑与导出。"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from app.config import settings
from app.services import results as rs


@dataclass
class FakeResult:
    response: dict
    status_code: int = 200
    latency_ms: int = 12
    attempts: int = 1
    prompt_tokens: int = 3
    completion_tokens: int = 4


def _response(text: str) -> dict:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }


@pytest.mark.asyncio
async def test_write_scan_and_export(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    monkeypatch.setattr(type(settings), "result_dir", property(lambda _: tmp_path / "results"))
    job_id = "job1"

    writer = rs.ResultWriter(job_id, flush_size=2)
    await writer.write_success(0, "a", FakeResult(_response("结果A")))
    await writer.write_success(1, "b", FakeResult(_response("结果B")))
    await writer.write_failure(2, "c", "HTTP 500: boom", 500, 3)
    await writer.flush()

    # --- 断点续跑状态 ---
    state = rs.scan_resume_state(job_id)
    assert state.done_indices == {0, 1, 2}
    assert (state.completed, state.failed) == (2, 1)
    assert (state.prompt_tokens, state.completion_tokens) == (6, 8)

    # --- 分页预览 ---
    page = rs.read_page(job_id, 0, 10)
    assert page["total"] == 2
    assert page["rows"][0]["output"] == "结果A"
    errors_page = rs.read_page(job_id, 0, 10, only_errors=True)
    assert errors_page["rows"][0]["error"].startswith("HTTP 500")

    # --- 导出 ---
    simple = b"".join(rs.iter_export(job_id, "simple")).decode()
    assert [json.loads(x)["custom_id"] for x in simple.strip().split("\n")] == ["a", "b"]

    raw_with_errors = b"".join(rs.iter_export(job_id, "raw", include_errors=True)).decode()
    assert len(raw_with_errors.strip().split("\n")) == 3

    csv_bytes = b"".join(rs.iter_export(job_id, "csv", include_errors=True))
    assert csv_bytes.startswith(b"\xef\xbb\xbf")  # Excel 需要 BOM 才能正确识别 UTF-8
    assert "结果A" in csv_bytes.decode("utf-8-sig")


@pytest.mark.asyncio
async def test_scan_skips_corrupt_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(type(settings), "result_dir", property(lambda _: tmp_path / "results"))
    job_id = "job2"
    path = rs.output_path(job_id)
    path.write_text(
        '{"_index": 0, "custom_id": "a", "response": {"status_code": 200, "body": {}}}\n'
        "{ 半行写到一半就断电了\n"
        '{"no_index": true}\n'
        '{"_index": 1, "custom_id": "b", "response": {"status_code": 200, "body": {}}}\n',
        encoding="utf-8",
    )
    state = rs.scan_resume_state(job_id)
    assert state.done_indices == {0, 1}
    assert state.completed == 2


@pytest.mark.asyncio
async def test_duplicate_index_counted_once(tmp_path, monkeypatch):
    """worker 崩溃重启后可能重复写同一条，统计不应重复计数。"""
    monkeypatch.setattr(type(settings), "result_dir", property(lambda _: tmp_path / "results"))
    job_id = "job3"
    line = '{"_index": 0, "custom_id": "a", "response": {"status_code": 200, "body": {}}}\n'
    rs.output_path(job_id).write_text(line + line, encoding="utf-8")
    assert rs.scan_resume_state(job_id).completed == 1


def test_delete_job_files_removes_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(type(settings), "result_dir", property(lambda _: tmp_path / "results"))
    job_id = "job4"
    rs.output_path(job_id).write_text("{}\n", encoding="utf-8")
    src = tmp_path / "input.jsonl"
    src.write_text("{}\n", encoding="utf-8")

    rs.delete_job_files(job_id, str(src))
    assert not (tmp_path / "results" / job_id).exists()
    assert not src.exists()
