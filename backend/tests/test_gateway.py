"""个人网关：模型列表解析、错误提示、虚拟模型配置。"""
from __future__ import annotations

import httpx
import pytest

from app.core.security import decrypt_secret
from app.services.gateway import (
    GatewayError,
    _extract_error,
    _parse_models,
    build_personal_config,
    fetch_models,
)
from app.services.settings_store import DEFAULTS


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def test_parse_openai_shape():
    payload = {"object": "list", "data": [{"id": "gpt-4o", "object": "model"}, {"id": "qwen3"}]}
    assert _parse_models(payload) == ["gpt-4o", "qwen3"]


def test_parse_tolerates_plain_string_list():
    assert _parse_models({"data": ["b", "a"]}) == ["a", "b"]
    assert _parse_models(["x"]) == ["x"]


def test_parse_dedupes_and_sorts():
    assert _parse_models({"data": [{"id": "b"}, {"id": "a"}, {"id": "b"}]}) == ["a", "b"]


@pytest.mark.parametrize("payload", [{}, {"data": None}, "nope", 42, {"data": [1, None]}])
def test_parse_garbage_yields_nothing(payload):
    assert _parse_models(payload) == []


def test_extract_error_reads_new_api_shape():
    resp = httpx.Response(401, json={"error": {"message": "Invalid token", "type": "new_api_error"}})
    assert _extract_error(resp) == "Invalid token"


def test_extract_error_falls_back_to_status():
    assert "500" in _extract_error(httpx.Response(500, text=""))


# --------------------------------------------------------------------------- #
# fetch_models：用 MockTransport 拦住真实网络
# --------------------------------------------------------------------------- #
def _patch_client(monkeypatch, handler):
    real_init = httpx.AsyncClient.__init__

    def fake_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)


@pytest.mark.asyncio
async def test_fetch_models_happy_path(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "qwen3-32b"}, {"id": "gpt-4o"}]})

    _patch_client(monkeypatch, handler)
    models = await fetch_models("https://gw.example.com/v1", "sk-user-token")

    assert models == ["gpt-4o", "qwen3-32b"]
    assert seen["url"] == "https://gw.example.com/v1/models"
    assert seen["auth"] == "Bearer sk-user-token"


@pytest.mark.asyncio
async def test_fetch_models_surfaces_gateway_message_on_401(monkeypatch):
    _patch_client(monkeypatch, lambda r: httpx.Response(
        401, json={"error": {"message": "Invalid token (request id: abc)"}}
    ))
    with pytest.raises(GatewayError) as exc:
        await fetch_models("https://gw.example.com/v1", "sk-bad")
    assert "Invalid token" in exc.value.message
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_fetch_models_empty_list_is_an_error(monkeypatch):
    """网关认了 token 但没给任何模型 —— 这对用户来说和失败没区别，要明说。"""
    _patch_client(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    with pytest.raises(GatewayError, match="没有被授予任何模型权限"):
        await fetch_models("https://gw.example.com/v1", "sk-x")


@pytest.mark.asyncio
async def test_fetch_models_non_json_hints_at_wrong_url(monkeypatch):
    _patch_client(monkeypatch, lambda r: httpx.Response(200, text="<html>nginx</html>"))
    with pytest.raises(GatewayError, match="/v1"):
        await fetch_models("https://gw.example.com", "sk-x")


@pytest.mark.asyncio
async def test_fetch_models_connection_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    _patch_client(monkeypatch, handler)
    with pytest.raises(GatewayError, match="无法连接网关"):
        await fetch_models("https://gw.example.com/v1", "sk-x")


# --------------------------------------------------------------------------- #
# 虚拟模型配置
# --------------------------------------------------------------------------- #
def test_build_personal_config_carries_token_and_limits():
    runtime = {**DEFAULTS, "user_gateway_base_url": "https://gw.example.com/v1",
               "user_gateway_max_concurrency": 6, "user_gateway_timeout": 120,
               "user_gateway_max_retries": 2, "user_gateway_max_tokens_cap": 4096}
    mc = build_personal_config("qwen3-32b", "sk-mine", runtime)

    assert mc.model_name == "qwen3-32b"
    assert mc.base_url == "https://gw.example.com/v1"
    assert mc.endpoint_path == "/chat/completions"
    assert mc.max_concurrency == 6
    assert mc.request_timeout == 120
    assert mc.max_retries == 2
    assert mc.max_tokens_cap == 4096
    # token 以加密形式携带，InferenceClient 走统一的解密路径
    assert mc.api_key_encrypted != "sk-mine"
    assert decrypt_secret(mc.api_key_encrypted) == "sk-mine"


def test_build_personal_config_falls_back_to_defaults():
    mc = build_personal_config("m", "sk", {})
    assert mc.base_url == DEFAULTS["user_gateway_base_url"].rstrip("/")
    assert mc.max_concurrency == 4


# --------------------------------------------------------------------------- #
# 个人模型的推理开关
# --------------------------------------------------------------------------- #
def test_personal_model_reasoning_is_optional_and_off_by_default():
    """默认允许用户自行开关推理，但开关本身是关的 —— 不主动打开就不改请求体。"""
    from app.services.inference import resolve_params

    mc = build_personal_config("m", "sk", {**DEFAULTS})
    assert mc.reasoning_mode == "optional"
    assert mc.reasoning_payload == {"enable_thinking": True}

    # 用户没打开开关：请求体里不出现推理字段
    assert resolve_params(mc, {}) == {}
    assert resolve_params(mc, {"reasoning": False}) == {}
    # 打开了才附加
    assert resolve_params(mc, {"reasoning": True}) == {"enable_thinking": True}


def test_admin_can_customize_reasoning_payload():
    """不同网关的推理字段不一样，后台可改。"""
    from app.services.inference import resolve_params

    mc = build_personal_config("m", "sk", {
        **DEFAULTS, "user_gateway_reasoning_payload": {"reasoning_effort": "high"},
    })
    assert resolve_params(mc, {"reasoning": True}) == {"reasoning_effort": "high"}


def test_personal_model_effort_options_come_from_settings():
    """网关的推理档位由后台配置，用户在建任务时挑一个。"""
    from app.services.inference import resolve_params

    mc = build_personal_config("m", "sk", {
        **DEFAULTS,
        "user_gateway_reasoning_payload": {"reasoning_effort": "$effort"},
        "user_gateway_reasoning_effort_options": ["low", "medium", "high"],
    })
    assert mc.reasoning_effort_options == ["low", "medium", "high"]
    assert resolve_params(mc, {"reasoning": True, "reasoning_effort": "high"}) == {
        "reasoning_effort": "high",
    }
    # 没选档位就用第一档
    assert resolve_params(mc, {"reasoning": True}) == {"reasoning_effort": "low"}


# --------------------------------------------------------------------------- #
# 按模型名的推理规则
# --------------------------------------------------------------------------- #
_RULES = {
    **DEFAULTS,
    "user_gateway_reasoning_payload": {"enable_thinking": True},
    "user_gateway_reasoning_effort_options": [],
    "user_gateway_reasoning_rules": [
        {
            "pattern": "qwen3-*",
            "payload": {"chat_template_kwargs": {"enable_thinking": True}},
            "effort_options": [],
        },
        {
            "pattern": "gpt-oss-*",
            "payload": {"reasoning_effort": "$effort"},
            "effort_options": ["low", "medium", "high"],
            "default_effort": "medium",
        },
        {"pattern": "*-instruct", "enabled": False},
    ],
}


def test_reasoning_rule_matches_model_name_with_wildcard():
    """同一个网关上不同系列的开法不一样，按模型名分别配。"""
    from app.services.inference import resolve_params

    qwen = build_personal_config("qwen3-32b", "sk", _RULES)
    assert resolve_params(qwen, {"reasoning": True}) == {
        "chat_template_kwargs": {"enable_thinking": True},
    }

    oss = build_personal_config("gpt-oss-120b", "sk", _RULES)
    assert oss.reasoning_effort_options == ["low", "medium", "high"]
    assert resolve_params(oss, {"reasoning": True}) == {"reasoning_effort": "medium"}
    assert resolve_params(oss, {"reasoning": True, "reasoning_effort": "high"}) == {
        "reasoning_effort": "high",
    }


def test_reasoning_rule_can_mark_a_model_as_unsupported():
    """有些模型压根不支持推理，规则里关掉，前端连开关都不显示。"""
    from app.services.inference import resolve_params

    mc = build_personal_config("qwen2.5-instruct", "sk", _RULES)
    assert mc.reasoning_mode == "off"
    assert resolve_params(mc, {"reasoning": True}) == {}


def test_unmatched_model_falls_back_to_gateway_default():
    from app.services.inference import resolve_params

    mc = build_personal_config("some-other-model", "sk", _RULES)
    assert resolve_params(mc, {"reasoning": True}) == {"enable_thinking": True}


def test_first_matching_rule_wins():
    """规则按顺序取第一条命中的，管理员靠排序决定优先级。"""
    runtime = {
        **DEFAULTS,
        "user_gateway_reasoning_rules": [
            {"pattern": "qwen3-32b", "payload": {"精确": True}},
            {"pattern": "qwen3-*", "payload": {"通配": True}},
        ],
    }
    mc = build_personal_config("qwen3-32b", "sk", runtime)
    assert mc.reasoning_payload == {"精确": True}


def test_global_switch_off_beats_every_rule():
    """管理员整体关掉推理时，规则一律不生效。"""
    mc = build_personal_config("qwen3-32b", "sk", {
        **_RULES, "user_gateway_reasoning_enabled": False,
    })
    assert mc.reasoning_mode == "off"


def test_admin_can_disable_reasoning_toggle_entirely():
    from app.services.inference import resolve_params

    mc = build_personal_config("m", "sk", {**DEFAULTS, "user_gateway_reasoning_enabled": False})
    assert mc.reasoning_mode == "off"
    # 关掉之后即使前端硬传 reasoning=true 也不生效
    assert resolve_params(mc, {"reasoning": True}) == {}
