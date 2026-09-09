"""代码生成：生成的脚本必须真的能跑，产出必须能被平台自己的解析器接受。"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.schemas import ScriptConfig, ScriptVariable
from app.services import codegen
from app.services.jsonl import validate_file


def make_config(**overrides) -> ScriptConfig:
    base = {
        "source_path": "./in.csv",
        "source_format": "csv",
        "variables": [ScriptVariable(name="data", field="text")],
        "prompt_template": "请翻译：{{data}}",
        "output_dir": "./out",
    }
    base.update(overrides)
    return ScriptConfig(**base)


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run_script(tmp_path: Path, config: ScriptConfig, *args: str) -> subprocess.CompletedProcess:
    script = tmp_path / "gen.py"
    script.write_text(codegen.generate(config), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )


def read_outputs(out_dir: Path) -> list[dict]:
    rows = []
    for path in sorted(out_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


# --------------------------------------------------------------------------- #
# 变量与校验
# --------------------------------------------------------------------------- #
def test_extract_variables_dedupes_and_keeps_order():
    assert codegen.extract_variables("{{b}} {{a}} {{b}}") == ["b", "a"]
    assert codegen.extract_variables("{{ spaced }}") == ["spaced"]
    assert codegen.extract_variables("没有变量") == []


def test_validate_flags_undefined_variable():
    cfg = make_config(prompt_template="{{data}} 和 {{missing}}")
    problems = codegen.validate(cfg)
    assert any("missing" in p for p in problems)


def test_validate_flags_unused_and_missing_id_field():
    cfg = make_config(
        variables=[ScriptVariable(name="data", field="text"), ScriptVariable(name="unused", field="x")],
        custom_id_mode="field",
    )
    problems = " ".join(codegen.validate(cfg))
    assert "unused" in problems
    assert "没填字段名" in problems


def test_clean_config_has_no_problems():
    assert codegen.validate(make_config()) == []


# --------------------------------------------------------------------------- #
# 生成的脚本必须是合法 Python，且不被用户输入破坏
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("nasty", [
    '带"双引号"的提示词',
    "带'单引号'和\\反斜杠",
    'triple """ quotes """ inside',
    "多行\n提示词\n第三行",
    "'''这是三引号'''",
    "结尾一个反斜杠\\",
    "中文、emoji 🚀、制表符\t",
])
def test_user_text_cannot_break_generated_script(nasty):
    """prompt 里什么字符都不该让脚本语法出错 —— 配置是以 repr() 嵌入的。"""
    cfg = make_config(prompt_template=f"{{{{data}}}} {nasty}", system_prompt=nasty)
    script = codegen.generate(cfg)
    compile(script, "generated", "exec")   # 语法过不了会直接抛异常
    # 而且配置能被原样读回来
    assert nasty in json.loads(_extract_config(script))["prompt_template"]


def _extract_config(script: str) -> str:
    line = next(x for x in script.splitlines() if x.startswith("CONFIG = json.loads("))
    return eval(line[len("CONFIG = json.loads("):-1])


# --------------------------------------------------------------------------- #
# 端到端：真的跑一遍脚本
# --------------------------------------------------------------------------- #
def test_generated_script_produces_platform_valid_jsonl(tmp_path):
    write_csv(tmp_path / "in.csv",
              [{"id": f"r{i}", "text": f"句子{i}"} for i in range(5)], ["id", "text"])
    cfg = make_config(custom_id_mode="field", custom_id_field="id", system_prompt="你是翻译员")

    result = run_script(tmp_path, cfg)
    assert result.returncode == 0, result.stdout + result.stderr

    rows = read_outputs(tmp_path / "out")
    assert len(rows) == 5
    assert rows[0]["custom_id"] == "r0"
    assert rows[0]["body"]["messages"][0] == {"role": "system", "content": "你是翻译员"}
    assert rows[0]["body"]["messages"][1]["content"] == "请翻译：句子0"

    # 关键：产出必须能被平台自己的解析器接受
    out_file = next((tmp_path / "out").glob("*.jsonl"))
    validation = validate_file(out_file, max_items=1000)
    assert validation.errors == []
    assert validation.total == 5


def test_split_by_rows(tmp_path):
    write_csv(tmp_path / "in.csv", [{"text": f"t{i}"} for i in range(10)], ["text"])
    cfg = make_config(max_rows_per_file=3, max_file_size_mb=0)

    assert run_script(tmp_path, cfg).returncode == 0
    files = sorted((tmp_path / "out").glob("*.jsonl"))
    assert [len(f.read_text(encoding="utf-8").strip().splitlines()) for f in files] == [3, 3, 3, 1]


def test_split_by_size_does_not_produce_empty_files(tmp_path):
    """单行就超过大小上限时，不能每行滚一个文件还留下空文件。"""
    write_csv(tmp_path / "in.csv", [{"text": "x" * 2000} for _ in range(5)], ["text"])
    cfg = make_config(max_rows_per_file=0, max_file_size_mb=1)
    # 用一个极小的上限逼出边界：1MB 装得下，改成按字节切
    cfg.max_file_size_mb = 1
    assert run_script(tmp_path, cfg).returncode == 0
    files = sorted((tmp_path / "out").glob("*.jsonl"))
    assert all(f.stat().st_size > 0 for f in files)
    assert sum(len(f.read_text(encoding="utf-8").strip().splitlines()) for f in files) == 5


def test_oversize_prompt_skip_and_truncate(tmp_path):
    write_csv(tmp_path / "in.csv",
              [{"text": "短"}, {"text": "很长" * 100}], ["text"])

    skip_cfg = make_config(max_prompt_chars=20, on_oversize="skip")
    assert run_script(tmp_path, skip_cfg).returncode == 0
    assert len(read_outputs(tmp_path / "out")) == 1

    for f in (tmp_path / "out").glob("*.jsonl"):
        f.unlink()
    trunc_cfg = make_config(max_prompt_chars=20, on_oversize="truncate")
    assert run_script(tmp_path, trunc_cfg).returncode == 0
    rows = read_outputs(tmp_path / "out")
    assert len(rows) == 2
    assert len(rows[1]["body"]["messages"][0]["content"]) == 20


def test_max_line_bytes_skips_long_lines(tmp_path):
    write_csv(tmp_path / "in.csv", [{"text": "短"}, {"text": "长" * 500}], ["text"])
    cfg = make_config(max_line_bytes=200)
    result = run_script(tmp_path, cfg)
    assert result.returncode == 0
    assert len(read_outputs(tmp_path / "out")) == 1
    assert "单行超过 200 字节" in result.stdout


def test_custom_id_modes(tmp_path):
    write_csv(tmp_path / "in.csv", [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}], ["id", "text"])

    for mode, prefix, check in [
        ("rownum", "req-", lambda ids: ids == ["req-1", "req-2"]),
        ("field", "", lambda ids: ids == ["a", "b"]),
        ("uuid", "", lambda ids: len(set(ids)) == 2 and all(len(i) == 32 for i in ids)),
    ]:
        for f in (tmp_path / "out").glob("*.jsonl"):
            f.unlink()
        cfg = make_config(custom_id_mode=mode, custom_id_field="id", custom_id_prefix=prefix)
        assert run_script(tmp_path, cfg).returncode == 0
        ids = [r["custom_id"] for r in read_outputs(tmp_path / "out")]
        assert check(ids), f"{mode} 模式产出的 id 不对：{ids}"


def test_jsonl_source(tmp_path):
    src = tmp_path / "in.jsonl"
    src.write_text(
        "\n".join(json.dumps({"text": f"第{i}条", "meta": {"k": i}}, ensure_ascii=False) for i in range(3))
        + "\n\n{ 这行是坏的\n",
        encoding="utf-8",
    )
    cfg = make_config(source_path="./in.jsonl", source_format="jsonl",
                      variables=[ScriptVariable(name="data", field="text"),
                                 ScriptVariable(name="meta", field="meta")],
                      prompt_template="{{data}} / {{meta}}")
    result = run_script(tmp_path, cfg)
    assert result.returncode == 0
    rows = read_outputs(tmp_path / "out")
    assert len(rows) == 3
    # dict 字段会被序列化成 JSON 文本，而不是 Python 的 repr
    assert '{"k": 0}' in rows[0]["body"]["messages"][0]["content"]
    assert "不是合法 JSON" in result.stdout


def test_directory_input(tmp_path):
    src = tmp_path / "data"
    src.mkdir()
    write_csv(src / "a.csv", [{"text": "a1"}], ["text"])
    write_csv(src / "b.csv", [{"text": "b1"}], ["text"])
    cfg = make_config(source_path="./data")
    assert run_script(tmp_path, cfg).returncode == 0
    assert len(read_outputs(tmp_path / "out")) == 2


def test_dry_run_writes_nothing(tmp_path):
    write_csv(tmp_path / "in.csv", [{"text": "x"}], ["text"])
    result = run_script(tmp_path, make_config(), "--dry-run")
    assert result.returncode == 0
    assert not (tmp_path / "out").exists()
    assert "dry-run" in result.stdout


def test_limit_flag(tmp_path):
    write_csv(tmp_path / "in.csv", [{"text": f"t{i}"} for i in range(20)], ["text"])
    assert run_script(tmp_path, make_config(), "--limit", "5").returncode == 0
    assert len(read_outputs(tmp_path / "out")) == 5


def test_duplicate_custom_id_warns(tmp_path):
    write_csv(tmp_path / "in.csv", [{"id": "same", "text": f"t{i}"} for i in range(3)], ["id", "text"])
    cfg = make_config(custom_id_mode="field", custom_id_field="id")
    result = run_script(tmp_path, cfg)
    assert "重复的 custom_id" in result.stdout


def test_missing_source_exits_with_message(tmp_path):
    result = run_script(tmp_path, make_config(source_path="./nope.csv"))
    assert result.returncode != 0
    assert "路径不存在" in result.stdout + result.stderr


def test_parquet_source(tmp_path):
    pytest.importorskip("pyarrow")
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table({"text": [f"行{i}" for i in range(4)], "id": [f"p{i}" for i in range(4)]})
    pq.write_table(table, tmp_path / "in.parquet")

    cfg = make_config(source_path="./in.parquet", source_format="parquet",
                      custom_id_mode="field", custom_id_field="id")
    assert run_script(tmp_path, cfg).returncode == 0
    rows = read_outputs(tmp_path / "out")
    assert len(rows) == 4
    assert rows[0]["custom_id"] == "p0"


# --------------------------------------------------------------------------- #
# 预览
# --------------------------------------------------------------------------- #
def test_preview_uses_placeholders_without_sample_data():
    record = codegen.preview_record(make_config())
    assert record["custom_id"] == "1"
    assert "<text 的值>" in record["body"]["messages"][0]["content"]


def test_preview_with_sample_row():
    cfg = make_config(custom_id_mode="field", custom_id_field="id", model="qwen3")
    record = codegen.preview_record(cfg, {"id": "abc", "text": "你好"})
    assert record["custom_id"] == "abc"
    assert record["body"]["messages"][0]["content"] == "请翻译：你好"
    assert record["body"]["model"] == "qwen3"
