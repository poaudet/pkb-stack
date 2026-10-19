"""OpenAI-compatible capture client. Request bodies never enable tool calling."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from ..config import Settings


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class CaptureLLMError(Exception):
    """Capture request failed without including owner or model content."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class LLMResult:
    text: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None


def _settings(settings: Settings) -> tuple[str, str, str, int, dict[str, str]]:
    base_url = settings.capture_llm_base_url or settings.llm_base_url
    api_key = settings.capture_llm_api_key or settings.llm_api_key
    model = settings.capture_llm_model or settings.llm_model
    timeout = max(1, settings.capture_llm_timeout)
    try:
        headers = json.loads(settings.capture_llm_extra_headers or "{}")
    except json.JSONDecodeError as exc:
        raise CaptureLLMError("invalid extra headers configuration") from exc
    if not isinstance(headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
    ):
        raise CaptureLLMError("invalid extra headers configuration")
    if any("\r" in k or "\n" in k or "\r" in v or "\n" in v for k, v in headers.items()):
        raise CaptureLLMError("invalid extra headers configuration")
    return base_url, api_key, model, timeout, headers


def _retry_after(exc: Exception, attempt: int) -> float:
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        return min(60.0, max(0.0, float(headers.get("retry-after", ""))))
    except (TypeError, ValueError):
        return float(min(30, 2 * attempt))


def call(settings: Settings, messages: list[dict[str, str]], before_request,
         *, max_attempts: int = 2, schema: dict | None = None) -> LLMResult:
    from openai import APIStatusError, BadRequestError, OpenAI

    base_url, api_key, model, timeout, headers = _settings(settings)
    if not model:
        raise CaptureLLMError("capture LLM is not configured")
    client = OpenAI(base_url=base_url or None, api_key=api_key or "not-needed",
                    timeout=timeout, max_retries=0, default_headers=headers or None)
    formats: list[dict | None] = []
    if schema is not None:
        formats.append({"type": "json_schema", "json_schema": {
            "name": "capture_plan", "schema": schema, "strict": False}})
    formats.extend([{"type": "json_object"}, None])
    attempt = 1
    while True:
        before_request()
        try:
            kwargs = {"response_format": formats[0]} if formats[0] else {}
            response = client.chat.completions.create(
                model=model, temperature=0.1, max_tokens=1500, messages=messages, **kwargs
            )
            usage = response.usage
            return LLMResult(
                response.choices[0].message.content or "", response.model or model,
                getattr(usage, "prompt_tokens", None) if usage else None,
                getattr(usage, "completion_tokens", None) if usage else None,
            )
        except BadRequestError as exc:
            if len(formats) > 1:
                formats.pop(0)
                continue
            raise CaptureLLMError(f"request failed: {type(exc).__name__}", 400) from exc
        except APIStatusError as exc:
            if exc.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                _sleep(_retry_after(exc, attempt))
                attempt += 1
                continue
            raise CaptureLLMError(
                f"request failed: HTTP {exc.status_code}", exc.status_code
            ) from exc
        except CaptureLLMError:
            raise
        except Exception as exc:
            raise CaptureLLMError(f"request failed: {type(exc).__name__}") from exc
