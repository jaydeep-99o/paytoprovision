import json

import pytest
from pydantic import BaseModel

from app.ai.llm import LLMError, extract_json, strip_reasoning
from tests.fakes import fake_llm


class Thing(BaseModel):
    name: str
    count: int


@pytest.mark.parametrize(
    "reply",
    [
        '{"name": "a", "count": 1}',
        '\n\n{"name": "a", "count": 1}',
        '<think>let me think {"name": "wrong"}</think>{"name": "a", "count": 1}',
        'Sure! Here you go:\n```json\n{"name": "a", "count": 1}\n```',
        'Here is the JSON: {"name": "a", "count": 1} Hope that helps.',
    ],
)
def test_extract_json_handles_reasoning_fences_and_chatter(reply):
    assert extract_json(reply) == {"name": "a", "count": 1}


def test_strip_reasoning_drops_unclosed_think():
    assert strip_reasoning('{"a": 1}<think>still thinking...') == '{"a": 1}'


def test_extract_json_rejects_non_objects():
    with pytest.raises(ValueError):
        extract_json("no json here")
    with pytest.raises(ValueError):
        extract_json("[1, 2, 3]")


def test_chat_json_uses_json_mode_and_validates():
    seen = []
    llm = fake_llm(['{"name": "a", "count": 2}'], seen)
    assert llm.chat_json([{"role": "user", "content": "hi"}], Thing) == Thing(name="a", count=2)
    assert seen[0]["response_format"] == {"type": "json_object"}
    assert "chat_template_kwargs" not in seen[0]  # thinking stays on by default


def test_chat_json_retries_once_with_the_error():
    seen = []
    llm = fake_llm(['{"name": "a"}', '{"name": "a", "count": 3}'], seen)
    assert llm.chat_json([{"role": "user", "content": "hi"}], Thing).count == 3
    retry_messages = seen[1]["messages"]
    assert retry_messages[-2] == {"role": "assistant", "content": '{"name": "a"}'}
    assert "count" in retry_messages[-1]["content"]  # validation error is fed back


def test_chat_json_gives_up_after_two_invalid_replies():
    llm = fake_llm(["not json", "still not json"])
    with pytest.raises(LLMError):
        llm.chat_json([{"role": "user", "content": "hi"}], Thing)


def test_thinking_can_be_disabled():
    seen = []
    llm = fake_llm([json.dumps({"name": "a", "count": 1})], seen)
    llm.chat_json([{"role": "user", "content": "hi"}], Thing, thinking=False)
    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_missing_api_key_is_an_error():
    from app.ai.llm import LLMClient

    with pytest.raises(LLMError):
        LLMClient("", model="m")
