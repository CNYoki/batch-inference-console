"""参数合并、限流与请求体构造。"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.models import ModelConfig
from app.services.inference import (
    RateLimiter,
    build_request_body,
    estimate_tokens,
    extract_output_text,
    resolve_params,
)


def make_mc(**overrides) -> ModelConfig:
    mc = ModelConfig(
        name="m", display_name="M", base_url="http://x/v1", model_name="real-model",
        endpoint_path="/chat/completions", extra_headers={}, enabled=True, admin_only=False,
        default_params={}, forced_params={}, allowed_param_keys=[],
        supports_temperature=True, supports_system_prompt=True, supports_json_mode=False,
        supports_tools=False, reasoning_mode="off", reasoning_payload={},
        max_concurrency=8, rpm_limit=0, tpm_limit=0, request_timeout=60, max_retries=1,
        max_tokens_cap=0, sort_order=0,
    )
    for k, v in overrides.items():
        setattr(mc, k, v)
    return mc


def test_user_params_override_defaults():
    mc = make_mc(default_params={"temperature": 0.7, "top_p": 1.0})
    out = resolve_params(mc, {"temperature": 0.1})
    assert out == {"temperature": 0.1, "top_p": 1.0}


def test_forced_params_win_over_user():
    mc = make_mc(default_params={"temperature": 0.7}, forced_params={"temperature": 0.0})
    assert resolve_params(mc, {"temperature": 1.9})["temperature"] == 0.0


def test_whitelist_blocks_other_keys():
    mc = make_mc(allowed_param_keys=["temperature"])
    out = resolve_params(mc, {"temperature": 0.2, "top_p": 0.5})
    assert out == {"temperature": 0.2}


def test_temperature_dropped_when_unsupported():
    mc = make_mc(supports_temperature=False)
    assert "temperature" not in resolve_params(mc, {"temperature": 0.5})


def test_max_tokens_capped():
    mc = make_mc(max_tokens_cap=100)
    assert resolve_params(mc, {"max_tokens": 9999})["max_tokens"] == 100


def test_model_and_stream_cannot_be_overridden_by_user():
    mc = make_mc()
    out = resolve_params(mc, {"model": "evil", "stream": True})
    assert "model" not in out and "stream" not in out


def test_reasoning_optional_only_applies_when_requested():
    mc = make_mc(reasoning_mode="optional", reasoning_payload={"reasoning_effort": "high"})
    assert "reasoning_effort" not in resolve_params(mc, {})
    assert resolve_params(mc, {"reasoning": True})["reasoning_effort"] == "high"


def test_reasoning_forced_always_applies():
    mc = make_mc(reasoning_mode="forced", reasoning_payload={"enable_thinking": True})
    assert resolve_params(mc, {})["enable_thinking"] is True


def test_build_body_injects_system_prompt_once():
    mc = make_mc()
    body = build_request_body(mc, {"messages": [{"role": "user", "content": "hi"}]}, {}, "你是助手")
    assert body["messages"][0] == {"role": "system", "content": "你是助手"}
    assert body["model"] == "real-model" and body["stream"] is False

    # 数据里已有 system 消息时不再注入，避免和用户数据打架
    already = build_request_body(
        mc, {"messages": [{"role": "system", "content": "原有"}, {"role": "user", "content": "hi"}]},
        {}, "你是助手",
    )
    assert [m["role"] for m in already["messages"]] == ["system", "user"]
    assert already["messages"][0]["content"] == "原有"


def test_per_item_params_beat_job_params_unless_forced():
    mc = make_mc(forced_params={"top_p": 0.1})
    body = build_request_body(
        mc, {"messages": [{"role": "user", "content": "x"}], "temperature": 0.9, "top_p": 0.9},
        {"temperature": 0.2, "top_p": 0.1}, None,
    )
    assert body["temperature"] == 0.9   # 单条显式值优先
    assert body["top_p"] == 0.1         # 强制参数仍然覆盖


def test_extract_output_text_handles_shapes():
    assert extract_output_text({"choices": [{"message": {"content": "hi"}}]}) == "hi"
    assert extract_output_text(
        {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}]}
    ) == "ab"
    assert extract_output_text({}) == ""


def test_estimate_tokens_is_positive():
    assert estimate_tokens({"messages": [{"role": "user", "content": "x" * 400}]}) == 100


@pytest.mark.asyncio
async def test_rate_limiter_allows_burst_then_throttles():
    limiter = RateLimiter(rpm=60)  # 桶容量 60，补充速率 1/秒

    # 令牌桶初始是满的，一整个突发批次应当立刻放行
    start = time.monotonic()
    for _ in range(60):
        await limiter.acquire()
    assert time.monotonic() - start < 0.5

    # 桶空之后就要按 1/秒 的速率等
    start = time.monotonic()
    await limiter.acquire()
    assert time.monotonic() - start >= 0.8


@pytest.mark.asyncio
async def test_rate_limiter_noop_when_unlimited():
    limiter = RateLimiter(0, 0)
    await asyncio.wait_for(asyncio.gather(*(limiter.acquire() for _ in range(200))), timeout=1)
