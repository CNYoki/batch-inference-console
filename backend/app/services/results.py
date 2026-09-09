"""结果文件的写入 / 断点续跑 / 导出。

每个任务一个目录 data/results/<job_id>/：
  output.jsonl  成功条目（含完整响应体）
  errors.jsonl  失败条目
两个文件都带 `_index` 字段，重跑时据此跳过已完成条目，实现断点续跑。
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import settings
from .inference import extract_output_text

log = logging.getLogger(__name__)

MAX_STORED_ERRORS = 2000  # 单任务写入数据库的失败样本上限


def job_dir(job_id: str) -> Path:
    d = settings.result_dir / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_path(job_id: str) -> Path:
    return job_dir(job_id) / "output.jsonl"


def errors_path(job_id: str) -> Path:
    return job_dir(job_id) / "errors.jsonl"


@dataclass
class ResumeState:
    done_indices: set[int]
    completed: int
    failed: int
    prompt_tokens: int
    completion_tokens: int


def scan_resume_state(job_id: str) -> ResumeState:
    """读已有结果文件，得出哪些条目已处理过。文件损坏的行直接跳过。"""
    done: set[int] = set()
    completed = failed = ptok = ctok = 0

    for path, is_error in ((output_path(job_id), False), (errors_path(job_id), True)):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                    idx = obj["_index"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
                if idx in done:
                    continue
                done.add(idx)
                if is_error:
                    failed += 1
                else:
                    completed += 1
                    usage = ((obj.get("response") or {}).get("body") or {}).get("usage") or {}
                    ptok += int(usage.get("prompt_tokens") or 0)
                    ctok += int(usage.get("completion_tokens") or 0)

    return ResumeState(done, completed, failed, ptok, ctok)


class ResultWriter:
    """带缓冲的追加写入器；worker 用它把结果批量落盘。"""

    def __init__(self, job_id: str, flush_size: int | None = None) -> None:
        self.job_id = job_id
        self.flush_size = flush_size or settings.result_flush_size
        self._out_buf: list[str] = []
        self._err_buf: list[str] = []
        self._lock = asyncio.Lock()

    async def write_success(self, index: int, custom_id: str, result: Any) -> None:
        line = json.dumps(
            {
                "_index": index,
                "custom_id": custom_id,
                "response": {"status_code": result.status_code, "body": result.response},
                "error": None,
                "latency_ms": result.latency_ms,
                "attempts": result.attempts,
            },
            ensure_ascii=False,
        )
        async with self._lock:
            self._out_buf.append(line)
            if len(self._out_buf) >= self.flush_size:
                await self._flush_locked()

    async def write_failure(
        self, index: int, custom_id: str, message: str, status_code: int | None, attempts: int
    ) -> None:
        line = json.dumps(
            {
                "_index": index,
                "custom_id": custom_id,
                "response": None,
                "error": {"status_code": status_code, "message": message},
                "attempts": attempts,
            },
            ensure_ascii=False,
        )
        async with self._lock:
            self._err_buf.append(line)
            if len(self._err_buf) >= self.flush_size:
                await self._flush_locked()

    async def _flush_locked(self) -> None:
        out, err = self._out_buf, self._err_buf
        self._out_buf, self._err_buf = [], []
        if out:
            await asyncio.to_thread(_append_lines, output_path(self.job_id), out)
        if err:
            await asyncio.to_thread(_append_lines, errors_path(self.job_id), err)

    async def flush(self) -> None:
        async with self._lock:
            await self._flush_locked()


def _append_lines(path: Path, lines: list[str]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
        fh.flush()


# --------------------------------------------------------------------------- #
# 读取 / 导出
# --------------------------------------------------------------------------- #
def read_page(job_id: str, offset: int = 0, limit: int = 50, only_errors: bool = False) -> dict[str, Any]:
    """结果分页预览：只解析需要的行，不把整个文件读进内存。"""
    path = errors_path(job_id) if only_errors else output_path(job_id)
    rows: list[dict[str, Any]] = []
    total = 0
    if path.exists():
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, raw in enumerate(fh):
                total += 1
                if i < offset or len(rows) >= limit:
                    continue
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                body = (obj.get("response") or {}).get("body") or {}
                rows.append(
                    {
                        "index": obj.get("_index"),
                        "custom_id": obj.get("custom_id"),
                        "output": extract_output_text(body) if body else None,
                        "error": (obj.get("error") or {}).get("message"),
                        "status_code": (obj.get("response") or obj.get("error") or {}).get("status_code"),
                        "attempts": obj.get("attempts"),
                        "usage": body.get("usage"),
                    }
                )
    return {"total": total, "offset": offset, "limit": limit, "rows": rows}


def iter_export(job_id: str, fmt: str, include_errors: bool = False) -> Iterator[bytes]:
    """流式导出，避免大文件一次性进内存。

    fmt: raw(原始 JSONL) / simple(custom_id+output 的 JSONL) / csv
    """
    if fmt == "csv":
        yield from _iter_csv(job_id, include_errors)
        return

    for path, is_error in _export_sources(job_id, include_errors):
        if not path.exists():
            continue
        with path.open("rb") as fh:
            if fmt == "raw":
                while chunk := fh.read(64 * 1024):
                    yield chunk
                continue
            for raw in fh:
                obj = _safe_load(raw)
                if obj is None:
                    continue
                yield (json.dumps(_simplify(obj, is_error), ensure_ascii=False) + "\n").encode("utf-8")


def _export_sources(job_id: str, include_errors: bool) -> list[tuple[Path, bool]]:
    sources = [(output_path(job_id), False)]
    if include_errors:
        sources.append((errors_path(job_id), True))
    return sources


def _safe_load(raw: bytes) -> dict[str, Any] | None:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _simplify(obj: dict[str, Any], is_error: bool) -> dict[str, Any]:
    body = (obj.get("response") or {}).get("body") or {}
    return {
        "custom_id": obj.get("custom_id"),
        "output": extract_output_text(body) if body else None,
        "error": (obj.get("error") or {}).get("message") if is_error or obj.get("error") else None,
        "usage": body.get("usage"),
    }


def _iter_csv(job_id: str, include_errors: bool) -> Iterator[bytes]:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["custom_id", "output", "error", "prompt_tokens", "completion_tokens"])
    yield b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")  # BOM 让 Excel 正确识别 UTF-8

    for path, is_error in _export_sources(job_id, include_errors):
        if not path.exists():
            continue
        with path.open("rb") as fh:
            for raw in fh:
                obj = _safe_load(raw)
                if obj is None:
                    continue
                item = _simplify(obj, is_error)
                usage = item.get("usage") or {}
                buf.seek(0)
                buf.truncate(0)
                writer.writerow(
                    [
                        item["custom_id"],
                        item["output"] or "",
                        item["error"] or "",
                        usage.get("prompt_tokens", ""),
                        usage.get("completion_tokens", ""),
                    ]
                )
                yield buf.getvalue().encode("utf-8")


def delete_job_files(job_id: str, input_path: str | None = None) -> None:
    import shutil

    shutil.rmtree(settings.result_dir / job_id, ignore_errors=True)
    if input_path:
        try:
            Path(input_path).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("删除输入文件失败 %s: %s", input_path, exc)


def result_sizes(job_id: str) -> dict[str, int]:
    def size(p: Path) -> int:
        return p.stat().st_size if p.exists() else 0

    return {"output_bytes": size(output_path(job_id)), "errors_bytes": size(errors_path(job_id))}
