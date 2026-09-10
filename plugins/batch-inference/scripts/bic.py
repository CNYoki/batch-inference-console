#!/usr/bin/env python3
"""bic —— 批量推理平台（Batch Inference Console）命令行。

只依赖 Python 3.9+ 标准库（读 Parquet 时需要 pyarrow）。主要给 Claude Code 的
batch-inference 插件调用，也可以直接在终端里用。

约定：命令的结果以 JSON 打到 stdout，过程信息打到 stderr；
退出码 0 = 成功，1 = 失败或校验没通过，2 = 用法错误。

凭证：到平台「个人设置 → API Token」生成，按页面上给出的命令写入
~/.config/bic/config.json，或设置环境变量 BIC_URL / BIC_TOKEN。
"""
from __future__ import annotations

import argparse
import codecs
import csv
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

VERSION = "0.1.0"
TOKEN_PREFIX = "bic_"
CONFIG_PATH = Path(
    os.environ.get("BIC_CONFIG") or (Path.home() / ".config" / "bic" / "config.json")
).expanduser()
MANIFEST_NAME = "bic_manifest.json"
PREP_CONFIG_NAME = "bic_prep_config.json"

TERMINAL = {"succeeded", "completed", "failed", "canceled"}
# 等待时遇到暂停也停下来：暂停的任务不会自己动，一直等没有意义
SETTLED = TERMINAL | {"paused"}

FORMAT_EXTS = {
    "csv": (".csv", ".tsv"),
    "jsonl": (".jsonl", ".ndjson", ".json"),
    "parquet": (".parquet", ".pq"),
}
RESULT_COLUMNS = ["result_status", "result_output", "result_error", "result_prompt_tokens", "result_completion_tokens"]

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


class CliError(Exception):
    """给人看的错误：打印信息后以退出码 1 结束。"""


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()


def info(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def clip(value, limit: int = 300):
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"…（共 {len(value)} 字符）"
    return value


# --------------------------------------------------------------------------- #
# 配置与 HTTP
# --------------------------------------------------------------------------- #
def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.endswith("/api"):
        url = url[:-4]
    if not re.match(r"^https?://", url):
        raise CliError(f"平台地址要以 http:// 或 https:// 开头：{url}")
    return url


def load_config() -> dict:
    cfg: dict = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CliError(f"配置文件 {CONFIG_PATH} 读取失败：{exc}") from None
    # 环境变量优先：CI 或临时切换平台时不必改文件
    for key, env in (("url", "BIC_URL"), ("token", "BIC_TOKEN")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def _describe_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        detail = payload.get("detail", payload)
    except (ValueError, AttributeError):
        detail = exc.reason
    if isinstance(detail, list):  # FastAPI 的 422 校验错误
        detail = "；".join(
            f"{'.'.join(str(p) for p in e.get('loc', [])[1:])}: {e.get('msg')}" for e in detail
        )
    msg = f"HTTP {exc.code}：{detail}"
    if exc.code == 401:
        msg += f"\n（token 无效或已过期：到平台「个人设置 → API Token」重新生成，再更新 {CONFIG_PATH}）"
    return msg


class Client:
    def __init__(self, url: str, token: str, timeout: float = 300):
        self.url = normalize_url(url)
        self.token = token
        self.timeout = timeout

    def open(self, method: str, path: str, *, params: dict | None = None, body=None,
             headers: dict | None = None, timeout: float | None = None):
        url = f"{self.url}/api{path}"
        if params:
            query = {k: v for k, v in params.items() if v is not None}
            if query:
                url += "?" + urllib.parse.urlencode(query)
        hdrs = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": f"bic/{VERSION}",
            **(headers or {}),
        }
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            raise CliError(_describe_http_error(exc)) from None
        except urllib.error.URLError as exc:
            raise CliError(f"连不上 {self.url}：{exc.reason}") from None

    def call(self, method: str, path: str, *, params: dict | None = None,
             json_body=None, timeout: float | None = None):
        body, headers = None, {}
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        with self.open(method, path, params=params, body=body, headers=headers, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8")) if raw else None


def get_client() -> Client:
    cfg = load_config()
    if not cfg.get("url") or not cfg.get("token"):
        raise CliError(
            "还没有配置平台地址和 token。\n"
            "到平台「个人设置 → API Token」新建一个，按页面提示在终端里执行配置命令；\n"
            f"或运行：python3 {Path(__file__).name} login --url https://<平台地址>"
        )
    return Client(cfg["url"], cfg["token"])


# --------------------------------------------------------------------------- #
# 本地数据读取（与平台生成的切分脚本保持同一套规则，custom_id 才对得上）
# --------------------------------------------------------------------------- #
def guess_format(path: Path) -> str | None:
    suffix = path.suffix.lower()
    for fmt, exts in FORMAT_EXTS.items():
        if suffix in exts:
            return fmt
    return None


def list_source_files(path_str: str, fmt: str | None, recursive: bool) -> tuple[list[Path], str]:
    path = Path(path_str).expanduser()
    if path.is_file():
        fmt = fmt or guess_format(path)
        if not fmt:
            raise CliError(f"认不出 {path.name} 的格式，请用 --format 指定 csv / jsonl / parquet")
        return [path], fmt
    if not path.is_dir():
        raise CliError(f"路径不存在：{path}")

    candidates = sorted(p for p in path.glob("**/*" if recursive else "*") if p.is_file())
    if not fmt:
        counts: dict[str, int] = {}
        for p in candidates:
            g = guess_format(p)
            if g:
                counts[g] = counts.get(g, 0) + 1
        if not counts:
            raise CliError(f"目录 {path} 下没有 csv / jsonl / parquet 文件")
        fmt = max(counts, key=lambda k: counts[k])
    # 与切分脚本一致：csv 只认 .csv
    exts = (".csv",) if fmt == "csv" else FORMAT_EXTS[fmt]
    files = [p for p in candidates if p.suffix.lower() in exts]
    if not files:
        raise CliError(f"目录 {path} 下没有找到 {'/'.join(exts)} 文件")
    return files, fmt


def sniff_encoding(path: Path) -> str:
    with path.open("rb") as fh:
        raw = fh.read(256 * 1024)
    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    for enc in ("utf-8", "gb18030"):
        try:
            codecs.getincrementaldecoder(enc)().decode(raw, final=False)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def sniff_delimiter(path: Path, encoding: str) -> str:
    with path.open("r", encoding=encoding, errors="replace", newline="") as fh:
        sample = fh.read(64 * 1024)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        return "\t" if path.suffix.lower() == ".tsv" else ","


def iter_rows(files: list[Path], fmt: str, encoding: str = "utf-8", delimiter: str = ","):
    for path in files:
        if fmt == "csv":
            with path.open("r", encoding=encoding, newline="", errors="replace") as fh:
                yield from csv.DictReader(fh, delimiter=delimiter)
        elif fmt == "jsonl":
            with path.open("r", encoding=encoding, errors="replace") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        yield obj
        else:
            try:
                import pyarrow.parquet as pq
            except ImportError:
                raise CliError("读取 parquet 需要 pyarrow：pip install pyarrow") from None
            for batch in pq.ParquetFile(path).iter_batches(batch_size=2048):
                yield from batch.to_pylist()


def field_value(row: dict, field: str) -> str:
    value = row.get(field)
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _type_name(value) -> str:
    if value is None or value == "":
        return "empty"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, (dict, list)):
        return "json"
    if isinstance(value, str):
        if re.fullmatch(r"-?\d+", value):
            return "int"
        if re.fullmatch(r"-?\d+\.\d*(e-?\d+)?", value, re.I):
            return "float"
        return "text"
    return type(value).__name__


# --------------------------------------------------------------------------- #
# 命令：登录
# --------------------------------------------------------------------------- #
def cmd_login(args) -> int:
    cfg = load_config() if CONFIG_PATH.exists() else {}
    url = args.url or cfg.get("url")
    if not url:
        raise CliError("请用 --url 指定平台地址，例如 --url https://console.example.com")
    url = normalize_url(url)

    token = args.token or os.environ.get("BIC_TOKEN")
    if not token:
        if not sys.stdin.isatty():
            raise CliError("没有可交互的终端，请通过环境变量 BIC_TOKEN 传入 token")
        info(f"到 {url}/profile 的「API Token」里新建一个 token，然后粘贴到这里（输入不回显）：")
        token = getpass.getpass("token: ").strip()
    elif args.token:
        info("[提示] --token 会留在 shell 历史里，更稳妥的做法是不带 --token 交互输入")
    if not token.startswith(TOKEN_PREFIX):
        raise CliError(f"这不像平台的 API Token（应以 {TOKEN_PREFIX} 开头）")

    me = Client(url, token).call("GET", "/auth/me")
    save_config({"url": url, "token": token})
    emit({"ok": True, "url": url, "username": me["username"], "config": str(CONFIG_PATH)})
    return 0


def cmd_whoami(args) -> int:
    client = get_client()
    me = client.call("GET", "/auth/me")
    usage = client.call("GET", "/usage")
    settings = client.call("GET", "/settings")
    emit({
        "url": client.url,
        "username": me["username"],
        "display_name": me.get("display_name"),
        "role": me["role"],
        "max_concurrent_jobs": me.get("max_concurrent_jobs") or "不限",
        "has_gateway_token": bool(me.get("llm_token_masked")),
        "storage": usage.get("storage"),
        # 切分数据时要照着这两个上限：单个分片不能超，每个分片会成为一个任务
        "limits": {
            "max_upload_mb": settings.get("max_upload_mb"),
            "max_items_per_job": settings.get("max_items_per_job"),
            "allow_new_jobs": settings.get("allow_new_jobs"),
        },
    })
    return 0


# --------------------------------------------------------------------------- #
# 命令：模型与 Prompt
# --------------------------------------------------------------------------- #
def cmd_models(args) -> int:
    opts = get_client().call("GET", "/models/options")
    shared = [
        {
            "id": m["id"],
            "name": m["name"],
            "display_name": m["display_name"],
            "description": m.get("description"),
            "supports_system_prompt": m["supports_system_prompt"],
            "supports_json_mode": m["supports_json_mode"],
            "reasoning_mode": m["reasoning_mode"],
            "reasoning_effort_options": m.get("reasoning_effort_options") or [],
            "reasoning_default_effort": m.get("reasoning_default_effort") or "",
            "allowed_param_keys": m.get("allowed_param_keys") or [],
            "default_params": m.get("default_params") or {},
            "max_concurrency": m["max_concurrency"],
            "max_tokens_cap": m["max_tokens_cap"],
        }
        for m in opts.get("shared", [])
    ]
    emit({
        "shared": shared,
        "personal": opts.get("personal", []),
        "personal_reasoning": opts.get("personal_reasoning", {}),
        "gateway_enabled": opts.get("gateway_enabled"),
        "gateway_label": opts.get("gateway_label"),
        "has_saved_gateway_token": opts.get("has_saved_token"),
        "personal_error": opts.get("personal_error"),
    })
    return 0


def _find_prompt(client: Client, key: str) -> dict:
    prompts = client.call("GET", "/prompts")
    for p in prompts:
        if key in (p["id"], p["name"]):
            return p
    names = "、".join(p["name"] for p in prompts) or "（还没有保存任何 Prompt）"
    raise CliError(f"找不到 Prompt「{key}」。现有：{names}")


def cmd_prompts(args) -> int:
    client = get_client()
    if args.name:
        emit(_find_prompt(client, args.name))
        return 0
    emit([
        {
            "id": p["id"],
            "name": p["name"],
            "description": p.get("description"),
            "variables": p.get("variables", []),
            "has_system_prompt": bool(p.get("system_prompt")),
            "prompt_template": clip(p["prompt_template"], 500),
        }
        for p in client.call("GET", "/prompts")
    ])
    return 0


# --------------------------------------------------------------------------- #
# 命令：本地数据探查
# --------------------------------------------------------------------------- #
def cmd_inspect(args) -> int:
    files, fmt = list_source_files(args.path, args.format, args.recursive)
    first = files[0]
    encoding = args.encoding or ("utf-8" if fmt == "parquet" else sniff_encoding(first))
    delimiter = args.delimiter or (sniff_delimiter(first, encoding) if fmt == "csv" else None)

    columns: dict[str, dict] = {}
    samples: list[dict] = []
    scanned = 0
    for row in iter_rows([first], fmt, encoding, delimiter or ","):
        if scanned >= args.scan:
            break
        scanned += 1
        for key, value in row.items():
            col = columns.setdefault(key, {"name": key, "types": set(), "empty": 0, "max_length": 0, "values": set()})
            col["types"].add(_type_name(value))
            text = field_value(row, key)
            if not text:
                col["empty"] += 1
            col["max_length"] = max(col["max_length"], len(text))
            if len(col["values"]) <= scanned:  # 只为判断唯一性，记到当前行数为止
                col["values"].add(text)
        if len(samples) < args.rows:
            samples.append({k: clip(field_value(row, k), 200) for k in row})

    total = None
    if not args.no_count:
        total = sum(1 for _ in iter_rows(files, fmt, encoding, delimiter or ","))

    hints = []
    if encoding == "utf-8-sig":
        hints.append("文件带 UTF-8 BOM：切分配置里 encoding 请填 utf-8-sig，否则第一列的列名会多出不可见字符")
    elif encoding == "gb18030":
        hints.append("文件不是 UTF-8，按 GB18030 读取：切分配置里 encoding 请填 gb18030")
    out_columns = []
    for col in columns.values():
        types = sorted(col["types"] - {"empty"}) or ["empty"]
        unique = scanned > 0 and col["empty"] == 0 and len(col["values"]) == scanned
        out_columns.append({
            "name": col["name"],
            "types": types,
            "empty_ratio": round(col["empty"] / scanned, 3) if scanned else None,
            "max_length": col["max_length"],
            "unique_in_sample": unique,
        })
        if unique and re.search(r"(^|_)(id|uuid|key|no)$", col["name"], re.I):
            hints.append(f"列「{col['name']}」在样本里非空且唯一，适合作为 custom_id（custom_id_mode=field）")

    result = {
        "path": str(Path(args.path).expanduser().resolve()),
        "format": fmt,
        "file_count": len(files),
        "files": [str(p) for p in files[:20]],
        "encoding": encoding,
        "csv_delimiter": delimiter,
        "rows": total,
        "scanned_rows": scanned,
        "columns": out_columns,
        "hints": hints,
    }
    if not args.schema_only:
        result["samples"] = samples
    emit(result)
    return 0


# --------------------------------------------------------------------------- #
# 命令：预处理（本地数据 → 平台输入 JSONL）
# --------------------------------------------------------------------------- #
def _count_lines(path: Path) -> int:
    with path.open("rb") as fh:
        return sum(1 for line in fh if line.strip())


def cmd_prep(args) -> int:
    client = get_client()
    cfg_path = Path(args.config).expanduser()
    try:
        cfg = json.loads(cfg_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"读取配置 {cfg_path} 失败：{exc}") from None

    if args.prompt:
        # 保存的 Prompt 提供模板；配置里显式写了的字段优先（新数据集的列名常常不一样）
        saved = _find_prompt(client, args.prompt)
        for key in ("system_prompt", "prompt_template", "variables"):
            if not cfg.get(key) and saved.get(key):
                cfg[key] = saved[key]

    for key in ("source_path", "source_format", "prompt_template"):
        if not cfg.get(key):
            raise CliError(f"配置缺少 {key}")
    # 转成绝对路径：生成的脚本以后在别的目录里跑也不会找错文件
    cfg["source_path"] = str(Path(cfg["source_path"]).expanduser().resolve())
    cfg["output_dir"] = str(Path(cfg.get("output_dir") or "./batch_input").expanduser().resolve())
    cfg.setdefault("output_prefix", "part")

    # 只把配置发给平台换回脚本；数据本身不离开本机
    preview = client.call("POST", "/tools/split-script", json_body=cfg)
    problems = preview.get("problems", [])
    if problems and not args.force:
        emit({
            "ok": False,
            "stage": "config",
            "problems": problems,
            "sample_line": preview.get("sample_line"),
            "hint": "先修正配置；确认这些问题可以忽略时加 --force",
        })
        return 1

    out_dir = Path(cfg["output_dir"])
    prefix = cfg["output_prefix"]
    stale = sorted(out_dir.glob(f"{prefix}_*.jsonl")) if out_dir.exists() else []
    if stale and not args.dry_run:
        if not args.overwrite:
            raise CliError(
                f"{out_dir} 里已有 {len(stale)} 个旧分片（{prefix}_*.jsonl），混在一起会被当成新数据上传。\n"
                "确认可以删除就加 --overwrite，或者换一个 output_dir / output_prefix"
            )
        for p in stale:
            p.unlink()

    out_dir.mkdir(parents=True, exist_ok=True)
    script_path = Path(args.script).expanduser() if args.script else out_dir / preview["script_name"]
    script_path.write_text(preview["script"], encoding="utf-8")
    # 存一份生效的配置，之后 join 结果时用同一套 custom_id 规则
    (out_dir / PREP_CONFIG_NAME).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    cmd = [sys.executable, str(script_path)]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    info("$ " + " ".join(cmd))
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if proc.stdout:
        info(proc.stdout.rstrip())
    if proc.stderr:
        info(proc.stderr.rstrip())

    files = []
    if not args.dry_run:
        files = [
            {"file": str(p), "rows": _count_lines(p), "size": human(p.stat().st_size)}
            for p in sorted(out_dir.glob(f"{prefix}_*.jsonl"))
        ]
    emit({
        "ok": proc.returncode == 0,
        "dry_run": args.dry_run,
        "limit": args.limit or None,
        "script": str(script_path),
        "prep_config": str(out_dir / PREP_CONFIG_NAME),
        "problems": problems,
        "sample_line": clip(preview.get("sample_line"), 2000),
        "files": files,
        "total_rows": sum(f["rows"] for f in files),
        "script_output_tail": proc.stdout[-3000:],
    })
    return 0 if proc.returncode == 0 else 1


# --------------------------------------------------------------------------- #
# 命令：上传
# --------------------------------------------------------------------------- #
def _upload_one(client: Client, path: Path) -> dict:
    boundary = "----bic" + uuid.uuid4().hex
    filename = path.name.replace('"', "_")
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    size = path.stat().st_size

    def body():
        yield head
        sent, last = 0, time.monotonic()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                sent += len(chunk)
                if time.monotonic() - last > 5:
                    info(f"  {path.name}: 已上传 {human(sent)} / {human(size)}")
                    last = time.monotonic()
                yield chunk
        yield tail

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(head) + size + len(tail)),
    }
    with client.open("POST", "/jobs/upload", body=body(), headers=headers, timeout=3600) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _expand_inputs(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            found = sorted(q for q in p.iterdir() if q.suffix.lower() in (".jsonl", ".ndjson"))
            if not found:
                raise CliError(f"目录 {p} 下没有 .jsonl 文件")
            files += found
        elif p.is_file():
            files.append(p)
        else:
            raise CliError(f"文件不存在：{p}")
    return files


def _manifest_path(args, files: list[Path] | None = None) -> Path:
    if getattr(args, "manifest", None):
        return Path(args.manifest).expanduser()
    if files:
        return files[0].resolve().parent / MANIFEST_NAME
    raise CliError("请指定 --manifest")


def _read_manifest(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"读取清单 {path} 失败：{exc}") from None


def _write_manifest(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_upload(args) -> int:
    client = get_client()
    files = _expand_inputs(args.files)
    manifest_path = _manifest_path(args, files)

    uploads = []
    for i, path in enumerate(files, 1):
        info(f"[{i}/{len(files)}] 上传 {path.name}（{human(path.stat().st_size)}）")
        res = _upload_one(client, path)
        uploads.append({
            "file": str(path.resolve()),
            "upload_id": res["upload_id"],
            "size": res["size"],
            "total_items": res["total_items"],
            "errors": res["errors"],
            "duplicate_custom_ids": res.get("duplicate_custom_ids", []),
            "first_item": {
                k: clip(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False), 600)
                for k, v in (res["preview"][:1] or [{}])[0].items()
            },
        })

    _write_manifest(manifest_path, {"url": client.url, "uploads": uploads, "jobs": []})
    bad = [u for u in uploads if u["errors"]]
    emit({
        "ok": not bad,
        "manifest": str(manifest_path),
        "total_items": sum(u["total_items"] for u in uploads),
        "uploads": uploads,
    })
    return 1 if bad else 0


# --------------------------------------------------------------------------- #
# 命令：试跑与提交
# --------------------------------------------------------------------------- #
def _resolve_model(client: Client, name: str, personal_only: bool) -> tuple[dict, str]:
    opts = client.call("GET", "/models/options")
    if not personal_only:
        for m in opts.get("shared", []):
            if name in (m["id"], m["name"], m["display_name"]):
                return {"model_source": "shared", "model_config_id": m["id"]}, m["display_name"]
    if name in opts.get("personal", []):
        return {"model_source": "personal", "personal_model": name}, f"{name}（{opts.get('gateway_label') or '个人网关'}）"

    available = [m["name"] for m in opts.get("shared", [])] + list(opts.get("personal", []))
    msg = f"找不到模型「{name}」。可用：{'、'.join(available) or '无'}"
    if opts.get("personal_error"):
        msg += f"\n个人网关模型拉取失败：{opts['personal_error']}"
    elif not opts.get("has_saved_token") and opts.get("gateway_enabled"):
        msg += "\n（要用个人网关的模型，先在平台「个人设置」里保存网关 token）"
    raise CliError(msg)


def _build_params(args) -> dict:
    params: dict = {}
    if args.params_file:
        params.update(json.loads(Path(args.params_file).expanduser().read_text("utf-8")))
    if args.system_prompt_file:
        params["system_prompt"] = Path(args.system_prompt_file).expanduser().read_text("utf-8")
    for key in ("system_prompt", "temperature", "top_p", "max_tokens", "seed"):
        value = getattr(args, key)
        if value is not None:
            params[key] = value
    if args.json_mode:
        params["json_mode"] = True
    if args.reasoning or args.reasoning_effort:
        params["reasoning"] = True
    if args.reasoning_effort:
        params["reasoning_effort"] = args.reasoning_effort
    if args.extra:
        try:
            params["extra"] = {**params.get("extra", {}), **json.loads(args.extra)}
        except json.JSONDecodeError as exc:
            raise CliError(f"--extra 不是合法 JSON：{exc}") from None
    return params


def _collect_uploads(args) -> tuple[list[dict], Path | None]:
    """--upload-id 直接给，或从 upload 生成的清单里取。"""
    if args.upload_id:
        return [{"upload_id": u, "file": None, "total_items": None, "errors": []} for u in args.upload_id], None
    path = _manifest_path(args)
    uploads = _read_manifest(path).get("uploads", [])
    if not uploads:
        raise CliError(f"清单 {path} 里没有上传记录")
    return uploads, path


def cmd_dry_run(args) -> int:
    client = get_client()
    uploads, _ = _collect_uploads(args)
    selection, label = _resolve_model(client, args.model, args.personal)
    payload = {"upload_id": uploads[0]["upload_id"], **selection, "params": _build_params(args), "item_index": args.item_index}
    res = client.call("POST", "/jobs/dry-run", json_body=payload, timeout=600)
    emit({
        "ok": res["ok"],
        "model": label,
        "custom_id": res.get("custom_id"),
        "item_index": res.get("item_index"),
        "status_code": res.get("status_code"),
        "latency_ms": res.get("latency_ms"),
        "content": clip(res.get("content"), 3000),
        "usage": res.get("usage"),
        "error": res.get("error"),
        "request_body": res.get("request_body"),
        "response": res.get("response") if not res["ok"] else None,
    })
    return 0 if res["ok"] else 1


def cmd_submit(args) -> int:
    client = get_client()
    uploads, manifest_path = _collect_uploads(args)
    bad = [u for u in uploads if u.get("errors")]
    if bad:
        raise CliError("以下文件校验没通过，不能提交：" + "、".join(str(u["file"]) for u in bad))

    selection, label = _resolve_model(client, args.model, args.personal)
    params = _build_params(args)
    n = len(uploads)
    plan = [
        {
            "name": args.name if n == 1 else f"{args.name} [{i}/{n}]",
            "upload_id": u["upload_id"],
            "file": u.get("file"),
            "total_items": u.get("total_items"),
        }
        for i, u in enumerate(uploads, 1)
    ]
    summary = {
        "model": label,
        "params": params,
        "concurrency": args.concurrency or "按模型默认",
        "priority": args.priority if args.priority is not None else "默认",
        "jobs": plan,
        "total_items": sum(p["total_items"] or 0 for p in plan),
    }
    if not args.yes:
        # 建任务会真实消耗额度：不带 --yes 只给出计划，确认后再提交
        emit({"submitted": False, "plan": summary, "hint": "确认无误后加 --yes 真正提交"})
        return 0

    manifest = _read_manifest(manifest_path) if manifest_path else None
    created = []
    for p in plan:
        body = {"name": p["name"], "upload_id": p["upload_id"], **selection, "params": params}
        if args.concurrency:
            body["concurrency"] = args.concurrency
        if args.priority is not None:
            body["priority"] = args.priority
        try:
            job = client.call("POST", "/jobs", json_body=body)
        except CliError as exc:
            emit({"submitted": bool(created), "created": created, "failed_at": p["name"], "error": str(exc)})
            return 1
        info(f"已提交 {job['name']} → {job['id']}")
        created.append({"id": job["id"], "name": job["name"], "status": job["status"],
                        "total_items": job["total_items"], "queue_position": job.get("queue_position")})
        if manifest is not None:
            manifest.setdefault("jobs", []).append(created[-1])
            _write_manifest(manifest_path, manifest)

    emit({"submitted": True, "model": label, "jobs": created, "manifest": str(manifest_path) if manifest_path else None})
    return 0


# --------------------------------------------------------------------------- #
# 命令：查询与控制
# --------------------------------------------------------------------------- #
def _job_ids(args) -> list[str]:
    ids = list(args.job_ids or [])
    if not ids and getattr(args, "manifest", None):
        ids = [j["id"] for j in _read_manifest(Path(args.manifest).expanduser()).get("jobs", [])]
    if not ids:
        raise CliError("请给出任务 ID，或用 --manifest 指向 submit 写过的清单")
    return ids


def _brief(job: dict) -> dict:
    return {
        "id": job["id"],
        "name": job["name"],
        "status": job["status"],
        "progress": f"{job.get('progress') or 0:.1f}%",
        "total_items": job["total_items"],
        "completed_items": job["completed_items"],
        "failed_items": job["failed_items"],
        "queue_position": job.get("queue_position"),
        "prompt_tokens": job.get("prompt_tokens"),
        "completion_tokens": job.get("completion_tokens"),
        "error": job.get("error"),
        "files_purged": bool(job.get("files_purged_at")),
    }


def cmd_jobs(args) -> int:
    res = get_client().call("GET", "/jobs", params={"page_size": args.limit, "status": args.status, "keyword": args.keyword})
    emit({"total": res["total"], "items": [_brief(j) for j in res["items"]]})
    return 0


def cmd_status(args) -> int:
    client = get_client()
    emit([_brief(client.call("GET", f"/jobs/{j}")) for j in _job_ids(args)])
    return 0


def cmd_wait(args) -> int:
    client = get_client()
    ids = _job_ids(args)
    started = time.monotonic()
    while True:
        jobs = [_brief(client.call("GET", f"/jobs/{j}")) for j in ids]
        stamp = time.strftime("%H:%M:%S")
        info(f"[{stamp}] " + "  ".join(f"{j['name']}: {j['status']} {j['progress']}" for j in jobs))
        if all(j["status"] in SETTLED for j in jobs):
            break
        if args.timeout and time.monotonic() - started >= args.timeout:
            emit({"done": False, "jobs": jobs, "hint": "还没结束，可以再次运行 wait 继续等"})
            return 3
        time.sleep(args.interval)
    ok = all(j["status"] in ("succeeded", "completed") for j in jobs)
    emit({"done": True, "ok": ok, "jobs": jobs})
    return 0 if ok else 1


def cmd_errors(args) -> int:
    rows = get_client().call("GET", f"/jobs/{args.job_id}/errors", params={"limit": args.limit})
    emit([{**r, "message": clip(r.get("message"), 500)} for r in rows])
    return 0


def cmd_control(args) -> int:
    client = get_client()
    action = {"cancel": "cancel", "pause": "pause", "resume": "resume", "retry-failed": "retry-failed"}[args.command]
    emit([_brief(client.call("POST", f"/jobs/{j}/{action}")) for j in _job_ids(args)])
    return 0


def cmd_download(args) -> int:
    client = get_client()
    out_dir = Path(args.output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = "csv" if args.format == "csv" else "jsonl"
    files = []
    for job_id in _job_ids(args):
        job = client.call("GET", f"/jobs/{job_id}")
        safe = re.sub(r"[^\w.\-]+", "_", job["name"]).strip("_")[:60] or "results"
        dest = out_dir / f"{safe}_{job_id[:8]}.{args.format}.{ext}"
        tmp = dest.with_name(dest.name + ".part")
        params = {"fmt": args.format, "include_errors": "false" if args.no_errors else "true"}
        with client.open("GET", f"/jobs/{job_id}/download", params=params, timeout=600) as resp, tmp.open("wb") as fh:
            shutil.copyfileobj(resp, fh, 1 << 20)
        os.replace(tmp, dest)
        info(f"{job['name']} → {dest}（{human(dest.stat().st_size)}）")
        files.append({"job_id": job_id, "name": job["name"], "status": job["status"], "file": str(dest.resolve())})
    emit({"files": files})
    return 0


# --------------------------------------------------------------------------- #
# 命令：把结果对回原始数据
# --------------------------------------------------------------------------- #
def _extract_content(body: dict) -> str | None:
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(content, list):  # 多段内容
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content


def _load_results(paths: list[str]) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if path.suffix.lower() == ".csv":
            raise CliError(f"{path.name} 是 CSV：join 需要 JSONL 结果（download --format simple 或 raw）")
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "response" in obj:  # raw 格式
                    body = (obj.get("response") or {}).get("body") or {}
                    item = {"output": _extract_content(body), "error": (obj.get("error") or {}).get("message"),
                            "usage": body.get("usage")}
                else:
                    item = {"output": obj.get("output"), "error": obj.get("error"), "usage": obj.get("usage")}
                cid = str(obj.get("custom_id"))
                # 同一条既失败过又重试成功时，保留成功的那条
                if cid not in results or (results[cid]["error"] and not item["error"]):
                    results[cid] = item
    return results


def cmd_join(args) -> int:
    cfg: dict = {}
    if args.config:
        cfg = json.loads(Path(args.config).expanduser().read_text("utf-8"))
    source = args.source or cfg.get("source_path")
    if not source:
        raise CliError("请用 --source 指定原始数据，或用 --config 指向 prep 生成的 bic_prep_config.json")
    mode = args.id_mode or cfg.get("custom_id_mode") or "rownum"
    id_field = args.id_field or cfg.get("custom_id_field")
    prefix = args.prefix if args.prefix is not None else cfg.get("custom_id_prefix", "")
    if mode == "uuid":
        raise CliError("custom_id 用的是随机 uuid，无法对回原始行。下次预处理请用 field 或 rownum 模式")
    if mode == "field" and not id_field:
        raise CliError("custom_id_mode=field 需要 --id-field")

    files, fmt = list_source_files(source, args.format or cfg.get("source_format"), cfg.get("recursive", False))
    encoding = args.encoding or cfg.get("encoding") or ("utf-8" if fmt == "parquet" else sniff_encoding(files[0]))
    delimiter = args.delimiter or cfg.get("csv_delimiter") or ","
    results = _load_results(args.results)

    out_path = Path(args.output).expanduser()
    out_fmt = "jsonl" if out_path.suffix.lower() in (".jsonl", ".ndjson") else "csv"
    stats = {"rows": 0, "ok": 0, "error": 0, "missing": 0}
    seen: set[str] = set()

    writer = None
    fh = out_path.open("w", encoding="utf-8-sig" if out_fmt == "csv" else "utf-8", newline="")
    try:
        for index, row in enumerate(iter_rows(files, fmt, encoding, delimiter)):
            stats["rows"] += 1
            if mode == "field":
                cid = prefix + (field_value(row, id_field) or f"row-{index + 1}")
            else:
                cid = prefix + str(index + 1)
            res = results.get(cid)
            if res is None:
                status = "missing"
            else:
                seen.add(cid)
                status = "error" if res["error"] else "ok"
            stats[status] += 1
            usage = (res or {}).get("usage") or {}
            extra = {
                "result_status": status,
                "result_output": (res or {}).get("output"),
                "result_error": (res or {}).get("error"),
                "result_prompt_tokens": usage.get("prompt_tokens"),
                "result_completion_tokens": usage.get("completion_tokens"),
            }
            if out_fmt == "jsonl":
                fh.write(json.dumps({**row, **extra}, ensure_ascii=False, default=str) + "\n")
            else:
                if writer is None:
                    writer = csv.DictWriter(fh, fieldnames=list(row.keys()) + RESULT_COLUMNS, extrasaction="ignore")
                    writer.writeheader()
                flat = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in row.items()}
                writer.writerow({**flat, **{k: ("" if v is None else v) for k, v in extra.items()}})
    finally:
        fh.close()

    emit({
        "output": str(out_path.resolve()),
        **stats,
        "results_loaded": len(results),
        "results_unmatched": len(set(results) - seen),
        "note": "missing 通常是预处理时被跳过的行（空 prompt、超长）或尚未跑完的任务",
    })
    return 0


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def _add_model_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--model", required=True, help="模型的 name / display_name / id，或个人网关模型名")
    p.add_argument("--personal", action="store_true", help="只在个人网关模型里找")
    p.add_argument("--system-prompt", help="任务级 system prompt（数据里已有 system 消息时一般不需要）")
    p.add_argument("--system-prompt-file")
    p.add_argument("--temperature", type=float)
    p.add_argument("--top-p", type=float)
    p.add_argument("--max-tokens", type=int)
    p.add_argument("--seed", type=int)
    p.add_argument("--json-mode", action="store_true", help="要求模型输出 JSON（模型需支持）")
    p.add_argument("--reasoning", action="store_true", help="开启推理/思考")
    p.add_argument("--reasoning-effort", help="推理档位，取值见 models 输出的 reasoning_effort_options")
    p.add_argument("--extra", help="额外请求参数，JSON 对象")
    p.add_argument("--params-file", help="从 JSON 文件读取参数（命令行参数优先）")


def _add_upload_source(p: argparse.ArgumentParser) -> None:
    p.add_argument("--upload-id", action="append", help="上传得到的 upload_id，可重复")
    p.add_argument("--manifest", help=f"upload 生成的清单（默认在数据目录下的 {MANIFEST_NAME}）")


def _add_job_ids(p: argparse.ArgumentParser) -> None:
    p.add_argument("job_ids", nargs="*", help="任务 ID")
    p.add_argument("--manifest", help="从 submit 写过的清单里取任务 ID")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bic", description="批量推理平台命令行")
    parser.add_argument("--version", action="version", version=f"bic {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="配置平台地址与 API Token")
    p.add_argument("--url")
    p.add_argument("--token", help="不建议：会留在 shell 历史里；缺省时交互输入或读 BIC_TOKEN")
    p.set_defaults(func=cmd_login)

    sub.add_parser("whoami", help="当前账号与存储用量").set_defaults(func=cmd_whoami)
    sub.add_parser("models", help="可用模型（公用 + 个人网关）").set_defaults(func=cmd_models)

    p = sub.add_parser("prompts", help="我的 Prompt")
    p.add_argument("name", nargs="?", help="给出名称或 ID 时显示完整内容")
    p.set_defaults(func=cmd_prompts)

    p = sub.add_parser("inspect", help="探查本地数据的列、类型、编码（纯本地，不联网）")
    p.add_argument("path")
    p.add_argument("--format", choices=["csv", "jsonl", "parquet"])
    p.add_argument("--encoding")
    p.add_argument("--delimiter")
    p.add_argument("--recursive", action="store_true")
    p.add_argument("--rows", type=int, default=3, help="输出几行样例（默认 3）")
    p.add_argument("--scan", type=int, default=1000, help="用前多少行推断列类型（默认 1000）")
    p.add_argument("--schema-only", action="store_true", help="只输出列信息，不输出任何数据值")
    p.add_argument("--no-count", action="store_true", help="不统计总行数（大文件更快）")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("prep", help="按配置生成切分脚本并在本地运行，产出上传用的 JSONL")
    p.add_argument("--config", required=True, help="切分配置 JSON（字段同平台「脚本生成」）")
    p.add_argument("--prompt", help="用「我的 Prompt」里的模板（名称或 ID）")
    p.add_argument("--dry-run", action="store_true", help="只统计不写文件")
    p.add_argument("--limit", type=int, default=0, help="只处理前 N 行")
    p.add_argument("--script", help="脚本保存位置（默认放在输出目录）")
    p.add_argument("--overwrite", action="store_true", help="删除输出目录里同前缀的旧分片")
    p.add_argument("--force", action="store_true", help="配置检查有问题也继续")
    p.set_defaults(func=cmd_prep)

    p = sub.add_parser("upload", help="上传 JSONL（文件或目录），平台逐行校验")
    p.add_argument("files", nargs="+")
    p.add_argument("--manifest", help=f"清单写到哪里（默认第一个文件所在目录的 {MANIFEST_NAME}）")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("dry-run", help="用上传文件里的一条真实请求一次，不建任务")
    _add_upload_source(p)
    _add_model_args(p)
    p.add_argument("--item-index", type=int, default=0)
    p.set_defaults(func=cmd_dry_run)

    p = sub.add_parser("submit", help="创建任务（不带 --yes 只显示计划）")
    _add_upload_source(p)
    _add_model_args(p)
    p.add_argument("--name", required=True)
    p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--priority", type=int)
    p.add_argument("--yes", action="store_true", help="确认提交")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("jobs", help="我的任务列表")
    p.add_argument("--status", help="逗号分隔，如 running,queued")
    p.add_argument("--keyword")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("status", help="任务状态")
    _add_job_ids(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("wait", help="轮询直到任务结束或暂停")
    _add_job_ids(p)
    p.add_argument("--interval", type=int, default=30)
    p.add_argument("--timeout", type=int, default=0, help="最多等多少秒，0 = 一直等；超时退出码 3")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("errors", help="失败条目")
    p.add_argument("job_id")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_errors)

    for name, text in (("cancel", "取消任务"), ("pause", "暂停任务"), ("resume", "恢复任务"),
                       ("retry-failed", "只重跑失败条目")):
        p = sub.add_parser(name, help=text)
        _add_job_ids(p)
        p.set_defaults(func=cmd_control)

    p = sub.add_parser("download", help="下载结果（任务结束后）")
    _add_job_ids(p)
    p.add_argument("--format", choices=["raw", "simple", "csv"], default="simple")
    p.add_argument("--no-errors", action="store_true", help="不包含失败条目")
    p.add_argument("-o", "--output-dir", default="./batch_results")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("join", help="把结果按 custom_id 合并回原始数据")
    p.add_argument("--results", nargs="+", required=True, help="download 得到的 JSONL（simple 或 raw）")
    p.add_argument("--config", help=f"prep 生成的 {PREP_CONFIG_NAME}，自动带出数据源与 custom_id 规则")
    p.add_argument("--source")
    p.add_argument("--format", choices=["csv", "jsonl", "parquet"])
    p.add_argument("--encoding")
    p.add_argument("--delimiter")
    p.add_argument("--id-mode", choices=["field", "rownum", "uuid"])
    p.add_argument("--id-field")
    p.add_argument("--prefix")
    p.add_argument("-o", "--output", required=True, help=".csv 或 .jsonl")
    p.set_defaults(func=cmd_join)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        info(f"[错误] {exc}")
        return 1
    except KeyboardInterrupt:
        info("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
