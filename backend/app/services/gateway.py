"""用户自带 token 的模型网关。

用户在「新建任务」里填自己的网关 token，后端拿它去 `{base_url}/models`
拉取该 token 有权限的模型 —— 权限由网关判定，平台不做二次授权。
执行任务时同样以该 token 调用网关，用量算在用户自己头上。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..models import ModelConfig
from .settings_store import DEFAULTS

log = logging.getLogger(__name__)

MODELS_TIMEOUT = 20.0
MAX_MODELS = 500


class GatewayError(Exception):
    """网关不可用或 token 无效。message 直接展示给用户。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _extract_error(resp: httpx.Response) -> str:
    """把网关的错误体转成一句人话。

    new-api / OpenAI 都是 {"error": {"message": ...}}，但不能假定一定如此。
    """
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200] or f"HTTP {resp.status_code}"

    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
        if isinstance(err, str):
            return err[:300]
        if data.get("message"):
            return str(data["message"])[:300]
    return f"HTTP {resp.status_code}"


async def fetch_models(base_url: str, token: str) -> list[str]:
    """用用户 token 拉取有权限的模型 id 列表。"""
    url = base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=MODELS_TIMEOUT) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.TimeoutException as exc:
        raise GatewayError(f"连接网关超时：{url}") from exc
    except httpx.HTTPError as exc:
        raise GatewayError(f"无法连接网关 {url}：{exc}") from exc

    if resp.status_code == 401:
        raise GatewayError(f"token 无效或已过期（网关返回：{_extract_error(resp)}）", 401)
    if resp.status_code == 403:
        raise GatewayError(f"该 token 无权访问模型列表（{_extract_error(resp)}）", 403)
    if resp.status_code >= 400:
        raise GatewayError(f"网关返回错误：{_extract_error(resp)}", resp.status_code)

    try:
        payload = resp.json()
    except ValueError as exc:
        raise GatewayError("网关返回的不是合法 JSON，请确认地址填到 /v1 为止") from exc

    models = _parse_models(payload)
    if not models:
        raise GatewayError("网关没有返回任何模型 —— 该 token 可能没有被授予任何模型权限")
    return models


def _parse_models(payload: Any) -> list[str]:
    """兼容 {"data":[{"id":…}]}（OpenAI 标准）与少数网关的 {"data":["id"]} 写法。"""
    if isinstance(payload, dict):
        rows = payload.get("data")
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = None
    if not isinstance(rows, list):
        return []

    out: list[str] = []
    seen: set[str] = set()
    for row in rows[:MAX_MODELS]:
        if isinstance(row, str):
            model_id = row
        elif isinstance(row, dict):
            model_id = row.get("id") or row.get("model") or row.get("name")
        else:
            continue
        if model_id and str(model_id) not in seen:
            seen.add(str(model_id))
            out.append(str(model_id))
    return sorted(out)


def build_personal_config(model_name: str, token: str, runtime: dict) -> ModelConfig:
    """为个人 token 任务拼一个「虚拟」模型配置。

    只在内存里存在，不入库 —— 它承载的是网关地址与用户 token，
    这样 InferenceClient 不必区分公用模型和个人模型两条代码路径。
    """
    from ..core.security import encrypt_secret

    return ModelConfig(
        name=f"__personal__:{model_name}",
        display_name=model_name,
        base_url=(runtime.get("user_gateway_base_url") or DEFAULTS["user_gateway_base_url"]).rstrip("/"),
        model_name=model_name,
        # InferenceClient 统一走解密路径，这里也加密一次保持一致
        api_key_encrypted=encrypt_secret(token),
        endpoint_path="/chat/completions",
        extra_headers={},
        enabled=True,
        admin_only=False,
        default_params={},
        forced_params={},
        allowed_param_keys=[],
        supports_temperature=True,
        supports_system_prompt=True,
        supports_json_mode=True,
        supports_tools=False,
        # 默认允许用户自行开关推理；开关本身默认关闭（JobParams.reasoning 默认 False），
        # 只有用户主动打开时才会把 reasoning_payload 合进请求体
        reasoning_mode=(
            "optional" if runtime.get("user_gateway_reasoning_enabled", True) else "off"
        ),
        reasoning_payload=dict(
            runtime.get("user_gateway_reasoning_payload")
            or DEFAULTS["user_gateway_reasoning_payload"]
        ),
        reasoning_effort_options=[
            str(o) for o in (runtime.get("user_gateway_reasoning_effort_options") or [])
        ],
        reasoning_default_effort=str(runtime.get("user_gateway_reasoning_default_effort") or ""),
        max_concurrency=int(runtime.get("user_gateway_max_concurrency") or 4),
        rpm_limit=0,
        tpm_limit=0,
        request_timeout=int(runtime.get("user_gateway_timeout") or 300),
        max_retries=int(runtime.get("user_gateway_max_retries") or 3),
        max_tokens_cap=int(runtime.get("user_gateway_max_tokens_cap") or 0),
        sort_order=0,
    )

