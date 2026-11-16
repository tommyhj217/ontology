"""Azure OpenAI 연결. 키·엔드포인트는 .env 에서 읽는다(코드/저장소에 넣지 않는다)."""
from __future__ import annotations

import json
import os
from typing import Type, TypeVar

from dotenv import find_dotenv, load_dotenv
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

# 사내에서 흔히 쓰는 변수명을 모두 받아준다(앞에 있는 이름이 우선).
_KEY = ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_KEY", "AZURE_API_KEY", "OPENAI_API_KEY")
_ENDPOINT = ("AZURE_OPENAI_ENDPOINT", "AZURE_ENDPOINT", "AZURE_API_BASE", "OPENAI_API_BASE")
_DEPLOYMENT = ("AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT_NAME",
               "AZURE_OPENAI_CHAT_DEPLOYMENT", "AZURE_DEPLOYMENT", "AZURE_OPENAI_MODEL")
_VERSION = ("AZURE_OPENAI_API_VERSION", "OPENAI_API_VERSION", "AZURE_API_VERSION")
DEFAULT_API_VERSION = "2024-10-21"


class LLMConfigError(RuntimeError):
    pass


def _env(names: tuple[str, ...]) -> str:
    for n in names:
        if os.getenv(n):
            return os.environ[n].strip()
    return ""


def config() -> dict:
    load_dotenv(find_dotenv(usecwd=True))
    cfg = {"key": _env(_KEY), "endpoint": _env(_ENDPOINT), "deployment": _env(_DEPLOYMENT),
           "api_version": _env(_VERSION) or DEFAULT_API_VERSION}
    missing = [names[0] for field, names in
               (("key", _KEY), ("endpoint", _ENDPOINT), ("deployment", _DEPLOYMENT)) if not cfg[field]]
    if missing:
        raise LLMConfigError(".env 에 다음 값이 필요합니다: " + ", ".join(missing)
                             + "  (.env.example 참고)")
    return cfg


def is_configured() -> bool:
    try:
        config()
        return True
    except LLMConfigError:
        return False


def _client(cfg: dict):
    from openai import AzureOpenAI, OpenAI
    endpoint = cfg["endpoint"].rstrip("/")
    if endpoint.endswith("/openai/v1"):       # Azure의 신형 v1 엔드포인트
        return OpenAI(api_key=cfg["key"], base_url=endpoint + "/")
    return AzureOpenAI(api_key=cfg["key"], azure_endpoint=endpoint, api_version=cfg["api_version"])


def chat(system: str, user: str) -> str:
    cfg = config()
    resp = _client(cfg).chat.completions.create(
        model=cfg["deployment"],
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    return resp.choices[0].message.content or ""


def _strip_defaults(node):
    if isinstance(node, dict):
        node.pop("default", None)
        for v in node.values():
            _strip_defaults(v)
    elif isinstance(node, list):
        for v in node:
            _strip_defaults(v)


def strict_format(schema: Type[BaseModel]) -> dict:
    """pydantic 모델 → strict json_schema response_format (strict 모드가 거부하는 default 제거)."""
    from openai.lib._parsing._completions import type_to_response_format_param
    fmt = type_to_response_format_param(schema)
    _strip_defaults(fmt)
    return fmt


def chat_structured(system: str, user: str, schema: Type[T]) -> T:
    """pydantic 스키마에 맞는 구조화 출력을 받는다.

    구조화 출력(json_schema)을 지원하지 않는 배포/API 버전이면 JSON 모드로 재시도한다.
    """
    from openai import BadRequestError
    cfg = config()
    client = _client(cfg)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        resp = client.chat.completions.create(model=cfg["deployment"], messages=messages,
                                              response_format=strict_format(schema))
        msg = resp.choices[0].message
        if getattr(msg, "refusal", None):
            raise RuntimeError(f"모델이 응답을 거부했습니다: {msg.refusal}")
        return schema.model_validate_json(msg.content or "{}")
    except BadRequestError:
        messages[0]["content"] += ("\n\n반드시 아래 JSON Schema를 따르는 JSON 객체 하나만 출력하라.\n"
                                   + json.dumps(schema.model_json_schema(), ensure_ascii=False))
        resp = client.chat.completions.create(model=cfg["deployment"], messages=messages,
                                              response_format={"type": "json_object"})
        return schema.model_validate_json(resp.choices[0].message.content or "{}")


def complete(model: str, messages: list[dict], tools: list[dict] | None = None) -> dict:
    """도구 호출을 지원하는 채팅 호출. 에이전트가 쓰는 공통 형태로 정규화해 돌려준다.

    반환: {"content": str, "tool_calls": [{"id", "name", "arguments"}], "usage": {"prompt", "completion", "cached"}}
    """
    cfg = config()
    kwargs = {"model": model, "messages": messages}
    if tools:
        kwargs["tools"] = tools
    resp = _client(cfg).chat.completions.create(**kwargs)
    msg = resp.choices[0].message
    usage = resp.usage
    details = getattr(usage, "prompt_tokens_details", None)
    return {
        "content": msg.content or "",
        "tool_calls": [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments or "{}"}
                       for tc in (msg.tool_calls or [])],
        "usage": {"prompt": getattr(usage, "prompt_tokens", 0) or 0,
                  "completion": getattr(usage, "completion_tokens", 0) or 0,
                  "cached": getattr(details, "cached_tokens", 0) or 0},
    }
