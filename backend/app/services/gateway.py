"""用户自带 token 的模型网关。

两种模式：

- 直连（默认）：用户在「新建任务」里填自己的网关 token，后端拿它去 `{base_url}/models`
  拉取该 token 有权限的模型 —— 权限由网关判定，平台不做二次授权。
  执行任务时同样以该 token 调用网关，用量算在用户自己头上。
- 独立计费：推理和计费分开。推理走管理员配置的推理接口和平台自己的 Key；
  用户填的是网关里 batch 分组的 Key，调不了 /v1，只用来标识向谁扣费，
  由 worker 按用量调计费接口扣费（见 services.billing）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..core.security import decrypt_secret
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


async def fetch_models(base_url: str, token: str, *, platform: bool = False) -> list[str]:
    """拉取 token 有权限的模型 id 列表。

    platform=True 表示用的是平台自己的推理 Key（独立计费模式），
    出错时提示找管理员，而不是让用户去换 token。
    """
    url = base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=MODELS_TIMEOUT) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.TimeoutException as exc:
        raise GatewayError(f"连接网关超时：{url}") from exc
    except httpx.HTTPError as exc:
        raise GatewayError(f"无法连接网关 {url}：{exc}") from exc

    if platform and resp.status_code in (401, 403):
        raise GatewayError(
            f"平台推理接口的 API Key 无效或无权访问模型列表，请联系管理员（{_extract_error(resp)}）",
            resp.status_code,
        )
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
        if platform:
            raise GatewayError("推理接口没有返回任何模型，请联系管理员检查推理接口配置")
        raise GatewayError("网关没有返回任何模型 —— 该 token 可能没有被授予任何模型权限")
    return models


# --------------------------------------------------------------------------- #
# 独立计费模式
# --------------------------------------------------------------------------- #
BILLING_PATH = "/api/batch/billing"
USAGE_PATH = "/api/usage/token/"


def billing_mode(runtime: dict) -> bool:
    """个人网关是否走「推理、计费分开」。"""
    return bool(runtime.get("user_gateway_billing_enabled"))


def inference_endpoint(runtime: dict, user_token: str) -> tuple[str, str]:
    """执行推理用的地址和 Key：独立计费时是平台的推理接口，否则是网关 + 用户 token。"""
    if billing_mode(runtime):
        key = decrypt_secret(runtime.get("user_gateway_inference_api_key_encrypted") or "") or ""
        return (runtime.get("user_gateway_inference_base_url") or "").rstrip("/"), key
    return (runtime.get("user_gateway_base_url") or DEFAULTS["user_gateway_base_url"]).rstrip("/"), user_token


def billing_endpoint(runtime: dict) -> tuple[str, str]:
    """计费接口地址和管理员访问令牌（nap_...）。没配齐时返回空串。"""
    token = decrypt_secret(runtime.get("user_gateway_billing_token_encrypted") or "") or ""
    return (runtime.get("user_gateway_billing_url") or "").strip(), token


def billing_config_error(runtime: dict) -> str | None:
    """独立计费模式下配置缺了哪样。返回 None 表示齐了。"""
    base, key = inference_endpoint(runtime, "")
    if not base:
        return "管理员尚未配置个人网关的推理接口地址"
    if runtime.get("user_gateway_inference_api_key_encrypted") and not key:
        return "推理接口 API Key 无法解密（SECRET_KEY 可能已变更），请管理员重新填写"
    url, token = billing_endpoint(runtime)
    if not url:
        return "管理员尚未配置个人网关的计费接口地址"
    if not token:
        return "管理员尚未配置计费接口的访问令牌，或令牌无法解密"
    return None


def usage_url(billing_url: str) -> str:
    """由计费接口地址推出同一网关的 Key 用量查询地址。

    计费接口一般就是 https://host/api/batch/billing；网关挂在子路径下时把前缀保留下来。
    """
    parts = urlsplit(billing_url)
    path = parts.path.rstrip("/")
    prefix = path[: -len(BILLING_PATH)] if path.endswith(BILLING_PATH) else ""
    return urlunsplit((parts.scheme, parts.netloc, prefix + USAGE_PATH, "", ""))


@dataclass
class KeyInfo:
    """用户 batch Key 的额度信息，只用来提示和过滤模型，不作为能否扣费的依据。"""

    name: str = ""
    # 网关内部额度单位（默认 500000 = $1）
    total_available: int | None = None
    unlimited: bool = False
    expires_at: int | None = None
    # None = 不限模型
    model_limits: list[str] | None = None


def _parse_model_limits(data: dict) -> list[str] | None:
    if not data.get("model_limits_enabled"):
        return None
    raw = data.get("model_limits")
    if isinstance(raw, dict):
        names = [str(k) for k, v in raw.items() if v]
    elif isinstance(raw, list):
        names = [str(x) for x in raw]
    elif isinstance(raw, str):
        names = [x.strip() for x in raw.split(",")]
    else:
        return None
    names = [n for n in names if n]
    return names or None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def check_billing_key(runtime: dict, key: str) -> KeyInfo | None:
    """用用户的 batch Key 查一次额度，顺带验证 Key 是否可用。

    Key 无效、已禁用、额度用尽时网关会拒绝，直接报给用户；
    查询接口本身不通（老版本网关没有、网络抖动）不拦，真扣费时还会再判一次。
    """
    billing_url, _ = billing_endpoint(runtime)
    if not billing_url:
        return None
    url = usage_url(billing_url)
    try:
        async with httpx.AsyncClient(timeout=MODELS_TIMEOUT) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        log.warning("查询 Key 额度失败 %s: %s", url, exc)
        return None

    if resp.status_code in (401, 403):
        raise GatewayError(f"Key 不可用：{_extract_error(resp)}", resp.status_code)
    if resp.status_code >= 400:
        log.warning("查询 Key 额度返回 HTTP %s: %s", resp.status_code, resp.text[:200])
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("success") is False or payload.get("code") is False:
        raise GatewayError(f"Key 不可用：{str(payload.get('message') or '网关拒绝')[:300]}")

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    return KeyInfo(
        name=str(data.get("name") or ""),
        total_available=_as_int(data.get("total_available")),
        unlimited=bool(data.get("unlimited_quota")),
        expires_at=_as_int(data.get("expires_at")),
        model_limits=_parse_model_limits(data),
    )


async def list_user_models(runtime: dict, token: str) -> tuple[list[str], KeyInfo | None]:
    """用户在个人网关下能选的模型，以及（独立计费时）他那个 Key 的额度信息。

    两种模式都用用户自己的 Key 去个人网关地址拉模型 —— 有哪些模型权限仍由网关判定；
    独立计费时只是执行推理改走平台的推理接口。
    """
    if billing_mode(runtime) and (problem := billing_config_error(runtime)):
        raise GatewayError(problem)
    info = await check_billing_key(runtime, token) if billing_mode(runtime) else None
    return await fetch_models(runtime["user_gateway_base_url"], token), info


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


def resolve_reasoning(model_name: str, runtime: dict) -> dict:
    """挑出该模型该用哪份推理配置。

    先按顺序找命中的模型名规则，没命中就用网关的默认配置 ——
    同一个网关上 Qwen3 和 GPT-OSS 的开法不一样，只有一份全局配置不够用。
    """
    default = {
        "enabled": bool(runtime.get("user_gateway_reasoning_enabled", True)),
        "payload": dict(
            runtime.get("user_gateway_reasoning_payload")
            or DEFAULTS["user_gateway_reasoning_payload"]
        ),
        "effort_options": [
            str(o) for o in (runtime.get("user_gateway_reasoning_effort_options") or [])
        ],
        "default_effort": str(runtime.get("user_gateway_reasoning_default_effort") or ""),
        # 网关默认配置不带关闭片段；需要的按模型名配规则
        "off_payload": {},
    }
    # 整个开关被管理员关掉时，规则也不必再看
    if not default["enabled"]:
        return default

    for rule in runtime.get("user_gateway_reasoning_rules") or []:
        pattern = str(rule.get("pattern") or "")
        if not pattern or not fnmatch(model_name, pattern):
            continue
        return {
            "enabled": bool(rule.get("enabled", True)),
            "payload": dict(rule.get("payload") or {}),
            "effort_options": [str(o) for o in (rule.get("effort_options") or [])],
            "default_effort": str(rule.get("default_effort") or ""),
            "off_payload": dict(rule.get("off_payload") or {}),
        }
    return default


def build_personal_config(model_name: str, token: str, runtime: dict) -> ModelConfig:
    """为个人 token 任务拼一个「虚拟」模型配置。

    只在内存里存在，不入库 —— 它承载的是网关地址与用户 token，
    这样 InferenceClient 不必区分公用模型和个人模型两条代码路径。
    """
    from ..core.security import encrypt_secret

    reasoning = resolve_reasoning(model_name, runtime)
    # 独立计费时推理用平台的 Key，用户的 token 只拿来扣费
    base_url, api_key = inference_endpoint(runtime, token)
    return ModelConfig(
        name=f"__personal__:{model_name}",
        display_name=model_name,
        base_url=base_url,
        model_name=model_name,
        # InferenceClient 统一走解密路径，这里也加密一次保持一致
        api_key_encrypted=encrypt_secret(api_key) if api_key else None,
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
        reasoning_mode="optional" if reasoning["enabled"] else "off",
        reasoning_payload=reasoning["payload"],
        reasoning_off_payload=reasoning["off_payload"],
        reasoning_effort_options=reasoning["effort_options"],
        reasoning_default_effort=reasoning["default_effort"],
        max_concurrency=int(runtime.get("user_gateway_max_concurrency") or 4),
        rpm_limit=0,
        tpm_limit=0,
        request_timeout=int(runtime.get("user_gateway_timeout") or 300),
        max_retries=int(runtime.get("user_gateway_max_retries") or 3),
        max_tokens_cap=int(runtime.get("user_gateway_max_tokens_cap") or 0),
        sort_order=0,
    )

