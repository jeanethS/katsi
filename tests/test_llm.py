"""Tests for LLMClient — all use a fake in-memory ollama client."""

from types import SimpleNamespace

import pytest

from mnemo_core.clients.llm import ExtractionError, LLMClient
from mnemo_core.models import Extraction


class _FakeChatResp:
    def __init__(self, content: str):
        self.message = SimpleNamespace(content=content)


class _FakeOllama:
    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.chat_calls: list[dict] = []

    def chat(self, model, messages, **kwargs):
        self.chat_calls.append({"model": model, "messages": messages, **kwargs})
        return _FakeChatResp(self.replies.pop(0))


def test_extract_happy_path():
    fake = _FakeOllama([
        '{"summary": "ok", "entities": [], "topics": [], "references": []}',
    ])
    c = LLMClient(client=fake)
    e = c.extract("doc text")
    assert isinstance(e, Extraction)
    assert e.summary == "ok"
    assert e.entities == []
    assert e.topics == []
    assert e.references == []
    assert len(fake.chat_calls) == 1


def test_extract_strips_fenced_json():
    fake = _FakeOllama([
        '```json\n{"summary": "s", "entities": [], "topics": [], "references": []}\n```',
    ])
    c = LLMClient(client=fake)
    e = c.extract("doc text")
    assert e.summary == "s"


def test_extract_retries_once_on_bad_json():
    fake = _FakeOllama([
        "not json {{",
        '{"summary": "ok", "entities": [], "topics": [], "references": []}',
    ])
    c = LLMClient(client=fake)
    e = c.extract("doc text")
    assert e.summary == "ok"
    assert len(fake.chat_calls) == 2


def test_extract_raises_after_two_failures():
    fake = _FakeOllama(["not json {{", "also not json ::"])
    c = LLMClient(client=fake)
    with pytest.raises(ExtractionError):
        c.extract("doc text")
    assert len(fake.chat_calls) == 2


def test_extract_validates_pydantic_shape():
    """Missing entities/topics/references triggers retry via ValidationError."""
    fake = _FakeOllama([
        '{"summary": "x"}',
        '{"summary": "full", "entities": [], "topics": [], "references": []}',
    ])
    c = LLMClient(client=fake)
    e = c.extract("doc text")
    assert e.summary == "full"
    assert len(fake.chat_calls) == 2


def test_chat_passes_messages():
    fake = _FakeOllama(["hello back"])
    c = LLMClient(client=fake)
    result = c.chat("hi")
    assert result == "hello back"
    assert len(fake.chat_calls) == 1
    call = fake.chat_calls[0]
    messages = call["messages"]
    assert len(messages) == 1
    assert messages[0]["content"] == "hi"


def test_chat_honors_model_and_max_tokens_override():
    fake = _FakeOllama(["ok"])
    c = LLMClient(client=fake)
    c.chat("hi", model="llama3.2:3b", max_tokens=42)
    call = fake.chat_calls[0]
    assert call["model"] == "llama3.2:3b"
    assert call["options"]["num_predict"] == 42
