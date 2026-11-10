"""Embedding 批量请求：上传校验、请求体、端点路由、向量结果的预览与导出。"""
from __future__ import annotations

import asyncio
import csv
import io
import json

import httpx
import pytest

from app.models import ModelConfig
from app.services import results as results_svc
from app.services.inference import (
    InferenceClient,
    InferenceResult,
    build_request_body,
    embeddings_endpoint,
    estimate_tokens,
    extract_output_text,
    extract_preview_text,
)
from app.services.jsonl import JsonlError, normalize_line

VECTOR_RESPONSE = {
    "object": "list",
    "model": "emb",
    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, -0.2, 0.3, 0.4, 0.5]}],
    "usage": {"prompt_tokens": 7, "total_tokens": 7},
}


def model_config(endpoint_path: str = "/chat/completions", **overrides) -> ModelConfig:
    values = {
        "name": "emb", "display_name": "emb", "base_url": "http://infer.test/v1", "model_name": "emb",
        "api_key_encrypted": None, "endpoint_path": endpoint_path, "extra_headers": {},
        "forced_params": {}, "supports_system_prompt": True,
        "max_concurrency": 1, "rpm_limit": 0, "tpm_limit": 0, "request_timeout": 5, "max_retries": 0,
    }
    return ModelConfig(**{**values, **overrides})


@pytest.mark.parametrize(
    "line",
    [
        {"custom_id": "a", "input": "你好"},
        {"custom_id": "a", "input": ["a", "b"]},
        {"custom_id": "a", "input": [1, 2, 3]},
        {"custom_id": "a", "input": [[1, 2], [3]]},
        {"custom_id": "a", "url": "/v1/embeddings", "body": {"input": "你好"}},
    ],
)
def test_accepts_embedding_input(line):
    item = normalize_line(json.dumps(line), 0)
    assert item.custom_id == "a"
    assert item.body["input"] == (line.get("input") or line["body"]["input"])
    assert "url" not in item.body


@pytest.mark.parametrize("value", ["", "   ", [], [""], [[]], [1, "a"], 5, None, {"a": 1}, [True]])
def test_rejects_invalid_embedding_input(value):
    with pytest.raises(JsonlError, match="input"):
        normalize_line(json.dumps({"custom_id": "a", "input": value}), 0)


def test_messages_line_stays_chat_even_with_input():
    item = normalize_line(json.dumps({"messages": [{"role": "user", "content": "hi"}], "input": "x"}), 0)
    body = build_request_body(model_config(), item.body, {}, None)
    assert body["stream"] is False
    assert estimate_tokens({"messages": [{"role": "user", "content": "12345678"}]}) == 2


def test_embedding_body_drops_chat_params_and_system_prompt():
    mc = model_config()
    mc.body_marker = {"batch": True}
    resolved = {"temperature": 0.7, "enable_thinking": False, "dimensions": 256, "encoding_format": "float"}
    body = build_request_body(
        mc, {"input": "你好", "model": "user-model", "encoding_format": "base64"}, resolved, "你是助手"
    )
    assert body == {
        "input": "你好", "model": "emb", "dimensions": 256, "encoding_format": "base64", "batch": True,
    }


def test_embedding_body_keeps_forced_params():
    mc = model_config(forced_params={"truncate": True, "dimensions": 128})
    body = build_request_body(mc, {"input": "x", "dimensions": 64}, {"truncate": True, "dimensions": 128}, None)
    assert body["truncate"] is True
    assert body["dimensions"] == 128


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/chat/completions", "/embeddings"),
        ("/v1/chat/completions/", "/v1/embeddings"),
        ("/completions", "/embeddings"),
        ("/embeddings", "/embeddings"),
        ("/my/custom", "/my/custom"),
    ],
)
def test_embeddings_endpoint(path, expected):
    assert embeddings_endpoint(path) == expected


def test_estimate_tokens_for_input():
    assert estimate_tokens({"input": "12345678"}) == 2
    assert estimate_tokens({"input": ["1234", "5678"]}) == 2
    assert estimate_tokens({"input": [[1, 2], [3]]}) == 3


def _client(seen: list[httpx.Request], mc: ModelConfig, response: dict | None = None) -> InferenceClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=response or VECTOR_RESPONSE)

    client = InferenceClient(mc)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def test_complete_routes_embedding_and_counts_usage():
    async def run() -> tuple[list[httpx.Request], InferenceResult]:
        seen: list[httpx.Request] = []
        async with _client(seen, model_config()) as client:
            return seen, await client.complete({"model": "emb", "input": "你好"})

    seen, result = asyncio.run(run())
    assert str(seen[0].url) == "http://infer.test/v1/embeddings"
    assert result.prompt_tokens == 7
    assert result.completion_tokens == 0


def test_chat_requests_keep_configured_endpoint():
    async def run() -> list[httpx.Request]:
        seen: list[httpx.Request] = []
        chat = {"choices": [{"message": {"content": "ok"}}]}
        async with _client(seen, model_config(), chat) as client:
            await client.complete({"model": "emb", "messages": [{"role": "user", "content": "hi"}]})
        return seen

    assert str(asyncio.run(run())[0].url) == "http://infer.test/v1/chat/completions"


def test_try_once_previews_vector():
    async def run() -> dict:
        async with _client([], model_config()) as client:
            return await client.try_once({"model": "emb", "input": "你好"})

    out = asyncio.run(run())
    assert out["ok"] is True
    assert out["content"] == "[1 条向量，5 维] 0.1, -0.2, 0.3, 0.4, …"
    assert out["usage"]["prompt_tokens"] == 7


def test_probe_sends_input_to_embedding_endpoint():
    async def run() -> tuple[list[httpx.Request], dict]:
        seen: list[httpx.Request] = []
        async with _client(seen, model_config("/embeddings")) as client:
            return seen, await client.probe()

    seen, out = asyncio.run(run())
    assert json.loads(seen[0].content) == {"model": "emb", "input": "ping"}
    assert out["ok"] is True
    assert out["detail"]["sample"].startswith("[1 条向量，5 维]")


def test_output_text_orders_multiple_vectors():
    response = {"data": [{"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0]}]}
    assert extract_output_text(response) == "[[1.0],[2.0]]"
    assert extract_preview_text(response) == "[2 条向量，1 维] 1.0, …"


def test_base64_vector():
    response = {"data": [{"index": 0, "embedding": "QUJD"}]}
    assert extract_output_text(response) == "QUJD"
    assert extract_preview_text(response) == "[1 条向量，base64，4 字符]"


def test_results_preview_short_export_full():
    async def write() -> None:
        writer = results_svc.ResultWriter("job-emb")
        await writer.write_success(
            0, "a", InferenceResult(response=VECTOR_RESPONSE, status_code=200, latency_ms=1, attempts=1, prompt_tokens=7),
        )
        await writer.flush()

    asyncio.run(write())

    page = results_svc.read_page("job-emb")
    assert page["rows"][0]["output"] == "[1 条向量，5 维] 0.1, -0.2, 0.3, 0.4, …"

    simple = json.loads(b"".join(results_svc.iter_export("job-emb", "simple")))
    assert json.loads(simple["output"]) == VECTOR_RESPONSE["data"][0]["embedding"]

    text = b"".join(results_svc.iter_export("job-emb", "csv")).decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert json.loads(rows[0]["output"]) == VECTOR_RESPONSE["data"][0]["embedding"]
    assert rows[0]["prompt_tokens"] == "7"

    state = results_svc.scan_resume_state("job-emb")
    assert (state.completed, state.prompt_tokens, state.completion_tokens) == (1, 7, 0)
