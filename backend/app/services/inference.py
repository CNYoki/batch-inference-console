"""OpenAI 兼容端点的推理客户端：限流、重试、参数合并。"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..core.security import decrypt_secret
from ..models import ModelConfig

log = logging.getLogger(__name__)

# 这些参数不允许用户从 JSONL 里带进来覆盖（由平台/模型配置决定）
_PROTECTED_BODY_KEYS = {"model", "stream", "stream_options"}

RETRIABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}

# reasoning_payload 里的档位占位符：出现在片段任意深度都会被换成用户选的档位，
# 这样 {"reasoning_effort": "$effort"}、{"thinking": {"budget_tokens": "$effort"}}
# 这些写法都能共用一套配置
EFFORT_PLACEHOLDER = "$effort"


class InferenceError(Exception):
    def __init__(self, message: str, status_code: int | None = None, retriable: bool = False):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retriable = retriable


@dataclass
class InferenceResult:
    response: dict[str, Any]
    status_code: int
    latency_ms: int
    attempts: int
    prompt_tokens: int = 0
    completion_tokens: int = 0


class RateLimiter:
    """RPM / TPM 双令牌桶。limit 为 0 时该维度不限流。"""

    def __init__(self, rpm: int = 0, tpm: int = 0) -> None:
        self.rpm = rpm
        self.tpm = tpm
        self._lock = asyncio.Lock()
        self._req_tokens = float(rpm)
        self._tok_tokens = float(tpm)
        self._last = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        if self.rpm:
            self._req_tokens = min(float(self.rpm), self._req_tokens + elapsed * self.rpm / 60.0)
        if self.tpm:
            self._tok_tokens = min(float(self.tpm), self._tok_tokens + elapsed * self.tpm / 60.0)

    async def acquire(self, estimated_tokens: int = 0) -> None:
        if not self.rpm and not self.tpm:
            return
        while True:
            async with self._lock:
                self._refill()
                need_req = self.rpm and self._req_tokens < 1
                need_tok = self.tpm and estimated_tokens and self._tok_tokens < estimated_tokens
                if not need_req and not need_tok:
                    if self.rpm:
                        self._req_tokens -= 1
                    if self.tpm and estimated_tokens:
                        self._tok_tokens -= estimated_tokens
                    return
                wait = 0.05
                if need_req:
                    wait = max(wait, (1 - self._req_tokens) * 60.0 / self.rpm)
                if need_tok:
                    wait = max(wait, (estimated_tokens - self._tok_tokens) * 60.0 / self.tpm)
            await asyncio.sleep(min(wait, 5.0))


def _fill_effort(value: Any, effort: str) -> Any:
    """递归替换 payload 里的 $effort 占位符。

    整个值就是占位符、且档位是纯数字时转成 int —— budget_tokens 这类
    要数字的网关才不会收到字符串。
    """
    if isinstance(value, str):
        if value == EFFORT_PLACEHOLDER:
            return int(effort) if effort.lstrip("-").isdigit() else effort
        return value.replace(EFFORT_PLACEHOLDER, effort)
    if isinstance(value, dict):
        return {k: _fill_effort(v, effort) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill_effort(v, effort) for v in value]
    return value


def resolve_reasoning_payload(mc: ModelConfig, effort: str | None = None) -> dict[str, Any]:
    """开启推理时要合进请求体的片段，档位占位符已填好。"""
    payload = dict(mc.reasoning_payload or {})
    options = [str(o) for o in (mc.reasoning_effort_options or [])]
    if not options:
        return payload
    if effort not in options:
        # 用户没选或选了名单外的值：退回配置的默认档，默认档也没配就取第一项
        default = mc.reasoning_default_effort or ""
        effort = default if default in options else options[0]
    return _fill_effort(payload, effort)


def _merge_payload(params: dict[str, Any], payload: dict[str, Any]) -> None:
    """把推理片段合进参数；chat_template_kwargs 这类嵌套字段跟已有值合并，别整个盖掉。"""
    for key, value in payload.items():
        if isinstance(value, dict) and isinstance(params.get(key), dict):
            params[key] = {**params[key], **value}
        else:
            params[key] = value


def resolve_params(mc: ModelConfig, user_params: dict[str, Any] | None) -> dict[str, Any]:
    """合并：模型默认值 → 用户覆盖（受白名单约束）→ 强制值。"""
    params: dict[str, Any] = dict(mc.default_params or {})
    user_params = dict(user_params or {})

    reasoning = user_params.pop("reasoning", None)
    # 只有模型声明了可选档位，reasoning_effort 才是控制键；
    # 否则留在 user_params 里当普通参数透传（用户可能是从「附加参数」直接写的）
    effort = user_params.pop("reasoning_effort", None) if mc.reasoning_effort_options else None
    allowed = set(mc.allowed_param_keys or [])

    for key, value in user_params.items():
        if value is None or key in _PROTECTED_BODY_KEYS:
            continue
        if allowed and key not in allowed:
            continue
        if key == "temperature" and not mc.supports_temperature:
            continue
        params[key] = value

    params.update(mc.forced_params or {})

    if mc.max_tokens_cap:
        for key in ("max_tokens", "max_completion_tokens"):
            if key in params and isinstance(params[key], int):
                params[key] = min(params[key], mc.max_tokens_cap)

    # 推理开关：开了附加开启片段；没开（模型不支持，或可选但用户没打开）附加关闭片段 ——
    # Qwen3 这类默认就会思考的模型，不明确发 enable_thinking: false 是关不掉的
    if mc.reasoning_mode == "forced" or (mc.reasoning_mode == "optional" and reasoning):
        _merge_payload(params, resolve_reasoning_payload(mc, effort))
    else:
        _merge_payload(params, dict(mc.reasoning_off_payload or {}))

    return params


def build_request_body(
    mc: ModelConfig, item_body: dict[str, Any], resolved: dict[str, Any], system_prompt: str | None
) -> dict[str, Any]:
    """把单条输入与任务级参数合成最终请求体。"""
    body: dict[str, Any] = {k: v for k, v in item_body.items() if k not in _PROTECTED_BODY_KEYS}

    messages = list(body.get("messages") or [])
    # 已经有 system 消息时不重复注入，避免与用户数据打架
    if system_prompt and mc.supports_system_prompt and not any(
        m.get("role") == "system" for m in messages
    ):
        messages = [{"role": "system", "content": system_prompt}, *messages]
    body["messages"] = messages

    # 任务级参数不覆盖单条里显式写死的字段，除非该字段被模型配置强制
    forced = set((mc.forced_params or {}).keys())
    for key, value in resolved.items():
        if key not in body or key in forced:
            body[key] = value

    body["model"] = mc.model_name
    body["stream"] = False
    return body


def estimate_tokens(body: dict[str, Any]) -> int:
    """粗略估算 token 数用于 TPM 限流（约 4 字符 = 1 token）。"""
    chars = 0
    for m in body.get("messages") or []:
        content = m.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            chars += sum(len(p.get("text", "")) for p in content if isinstance(p, dict))
    return max(1, chars // 4)


class InferenceClient:
    """按模型配置构建，复用同一个 httpx 连接池。"""

    def __init__(self, mc: ModelConfig) -> None:
        self.mc = mc
        self.url = mc.base_url.rstrip("/") + "/" + mc.endpoint_path.lstrip("/")
        self.limiter = RateLimiter(mc.rpm_limit, mc.tpm_limit)

        headers = {"Content-Type": "application/json", **(mc.extra_headers or {})}
        api_key = decrypt_secret(mc.api_key_encrypted)
        if mc.api_key_encrypted and api_key is None:
            raise InferenceError("模型 API Key 解密失败（SECRET_KEY 可能已变更），请在后台重新填写")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        limits = httpx.Limits(
            max_connections=max(mc.max_concurrency * 2, 16),
            max_keepalive_connections=max(mc.max_concurrency, 8),
        )
        self._client = httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(mc.request_timeout, connect=15.0), limits=limits
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> InferenceClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def complete(self, body: dict[str, Any]) -> InferenceResult:
        est = estimate_tokens(body)
        last_error: InferenceError | None = None
        started = time.monotonic()

        for attempt in range(1, self.mc.max_retries + 2):
            await self.limiter.acquire(est)
            try:
                resp = await self._client.post(self.url, json=body)
            except httpx.TimeoutException as exc:
                last_error = InferenceError(f"请求超时: {exc}", None, retriable=True)
            except httpx.HTTPError as exc:
                last_error = InferenceError(f"网络错误: {exc}", None, retriable=True)
            else:
                if resp.status_code < 400:
                    try:
                        data = resp.json()
                    except ValueError:
                        last_error = InferenceError(
                            f"响应不是合法 JSON: {resp.text[:300]}", resp.status_code, retriable=True
                        )
                    else:
                        usage = data.get("usage") or {}
                        return InferenceResult(
                            response=data,
                            status_code=resp.status_code,
                            latency_ms=int((time.monotonic() - started) * 1000),
                            attempts=attempt,
                            prompt_tokens=int(usage.get("prompt_tokens") or 0),
                            completion_tokens=int(usage.get("completion_tokens") or 0),
                        )
                else:
                    retriable = resp.status_code in RETRIABLE_STATUS
                    last_error = InferenceError(
                        f"HTTP {resp.status_code}: {resp.text[:500]}", resp.status_code, retriable
                    )
                    if retriable and (ra := resp.headers.get("Retry-After")):
                        # Retry-After 也可能是 HTTP 日期格式，解析不了就退回指数退避
                        with contextlib.suppress(ValueError):
                            await asyncio.sleep(min(float(ra), 60.0))

            if last_error and not last_error.retriable:
                break
            if attempt <= self.mc.max_retries:
                backoff = min(2 ** (attempt - 1), 30) * (0.5 + random.random())
                await asyncio.sleep(backoff)

        raise last_error or InferenceError("未知错误")

    async def try_once(self, body: dict[str, Any]) -> dict[str, Any]:
        """试跑一条：只发一次、不重试、失败也不抛异常。

        用户正对着弹窗等结果，重试三次再报错太慢；而且这里要的就是
        原始状态码和响应正文 —— 哪里写错了让他自己看最直接。
        """
        started = time.monotonic()
        try:
            resp = await self._client.post(self.url, json=body)
        except httpx.TimeoutException as exc:
            return {"ok": False, "latency_ms": _elapsed_ms(started), "error": f"请求超时: {exc}"}
        except httpx.HTTPError as exc:
            return {"ok": False, "latency_ms": _elapsed_ms(started), "error": f"网络错误: {exc}"}

        out: dict[str, Any] = {
            "ok": resp.status_code < 400,
            "status_code": resp.status_code,
            "latency_ms": _elapsed_ms(started),
        }
        try:
            data = resp.json()
        except ValueError:
            out["ok"] = False
            out["error"] = f"响应不是合法 JSON: {resp.text[:1000]}"
            return out

        out["response"] = data
        if out["ok"]:
            out["content"] = extract_output_text(data)
            out["usage"] = data.get("usage") or {}
        else:
            # 网关的报错信息通常嵌在 error.message 里，挑出来当主提示
            err = data.get("error") if isinstance(data, dict) else None
            message = err.get("message") if isinstance(err, dict) else None
            out["error"] = f"HTTP {resp.status_code}: {message or json.dumps(data, ensure_ascii=False)[:1000]}"
        return out

    async def probe(self) -> dict[str, Any]:
        """后台「测试连通性」用：发一条最小请求。"""
        body = {
            "model": self.mc.model_name,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
            "stream": False,
        }
        body.update({k: v for k, v in (self.mc.forced_params or {}).items()})
        started = time.monotonic()
        resp = await self._client.post(self.url, json=body)
        latency = int((time.monotonic() - started) * 1000)
        ok = resp.status_code < 400
        detail: Any
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text[:500]
        return {
            "ok": ok,
            "status_code": resp.status_code,
            "latency_ms": latency,
            "url": self.url,
            "detail": detail if not ok else _brief(detail),
        }


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _brief(data: Any) -> Any:
    """成功探测只回传关键信息，避免把整段回复塞进后台页面。"""
    if not isinstance(data, dict):
        return data
    choices = data.get("choices") or []
    text = ""
    if choices and isinstance(choices[0], dict):
        text = (choices[0].get("message") or {}).get("content") or ""
    return {"model": data.get("model"), "usage": data.get("usage"), "sample": str(text)[:200]}


def extract_output_text(response: dict[str, Any]) -> str:
    """从 OpenAI 兼容响应里取出正文，供导出 CSV / 预览使用。"""
    choices = response.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return choices[0].get("text") or ""
