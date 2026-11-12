"""Chat client for Nebius Token Factory (OpenAI-compatible HTTP API) plus a strict-JSON helper.

Talks to `/chat/completions` with httpx directly rather than the OpenAI SDK: the SDK's
compiled dependencies don't load on every dev machine, and we need provider-specific
fields (`chat_template_kwargs`, `reasoning_content`) anyway.

Nemotron returns its reasoning in `reasoning_content`, separate from `content`, but
`extract_json` still strips any `<think>` blocks in case a model inlines them.
"""

import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CODE_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class LLMError(Exception):
    pass


def strip_reasoning(text: str) -> str:
    text = _THINK_BLOCK.sub("", text or "")
    # An unclosed <think> (truncated output): drop everything up to the last </think> or the tag itself.
    if "</think>" in text.lower():
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
    return re.sub(r"<think>.*", "", text, flags=re.DOTALL | re.IGNORECASE).strip()


def extract_json(text: str) -> dict[str, Any]:
    """Parse the JSON object out of a model reply that may include reasoning, fences or chatter."""
    text = strip_reasoning(text)
    fenced = _CODE_FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in reply") from None
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("reply is JSON but not an object")
    return value


class LLMClient:
    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str = "https://api.tokenfactory.nebius.com/v1/",
        timeout: float = 120.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        on_call: Callable[[], None] | None = None,
    ):
        if not api_key:
            raise LLMError("NEBIUS_API_KEY is not set")
        self.model = model
        self.max_retries = max_retries
        self._on_call = on_call  # e.g. budget.take; raises to refuse the call
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **kwargs: Any) -> "LLMClient":
        from app import budget

        s = settings or get_settings()
        kwargs.setdefault("on_call", lambda: budget.take(budget.AI))
        return cls(s.nebius_api_key.get_secret_value(), model=s.nebius_model, base_url=s.nebius_base_url, **kwargs)

    def __repr__(self) -> str:
        return f"LLMClient(model={self.model!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        thinking: bool = True,
        max_tokens: int = 4000,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """One chat completion. Returns the assistant message dict (content, tool_calls, ...)."""
        if self._on_call:
            self._on_call()
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = tools
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if not thinking:
            body["chat_template_kwargs"] = {"enable_thinking": False}

        for attempt in range(1, self.max_retries + 2):
            try:
                resp = self._http.post("chat/completions", json=body)
            except httpx.TransportError as exc:
                if attempt <= self.max_retries:
                    time.sleep(2**attempt)
                    continue
                raise LLMError(f"AI service unreachable ({type(exc).__name__})") from exc
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt <= self.max_retries:
                log.warning("LLM call failed (%s); retry %d/%d", resp.status_code, attempt, self.max_retries)
                time.sleep(2**attempt)
                continue
            if resp.status_code >= 400:
                raise LLMError(f"AI service error {resp.status_code}: {resp.text[:300]}")
            choice = resp.json()["choices"][0]
            if choice.get("finish_reason") == "length" and not choice["message"].get("tool_calls"):
                log.warning("LLM reply hit max_tokens=%d", max_tokens)
            return choice["message"]
        raise LLMError("AI service kept failing")  # pragma: no cover - loop always returns or raises

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        schema: type[T],
        *,
        thinking: bool = True,
        max_tokens: int = 6000,
    ) -> T:
        """Ask for a JSON object matching `schema`. On invalid JSON or a schema mismatch,
        tells the model what was wrong and retries once."""
        convo = list(messages)
        last_error = ""
        for attempt in (1, 2):
            message = self.chat(convo, json_mode=True, thinking=thinking, max_tokens=max_tokens)
            content = message.get("content") or ""
            try:
                return schema.model_validate(extract_json(content))
            except (ValueError, ValidationError) as exc:
                last_error = str(exc)[:800]
                log.warning("LLM returned invalid JSON (attempt %d): %s", attempt, last_error[:200])
                convo = convo + [
                    {"role": "assistant", "content": content[:4000]},
                    {
                        "role": "user",
                        "content": "That reply was not valid for the required JSON schema:\n"
                        f"{last_error}\nReply again with ONLY the corrected JSON object.",
                    },
                ]
        raise LLMError(f"AI returned invalid JSON twice: {last_error[:200]}")
