from __future__ import annotations

import os

import pytest

from mnemo_core.config import Settings
from mnemo_core.models import Chunk, ContextBundle, FileHit
from mnemo_core.synth import (
    AutoSynthesizer,
    CloudSynthesizer,
    LocalSynthesizer,
    ReturnOnlySynthesizer,
    SynthConfigError,
    SynthUnavailableError,
    build_synthesizer,
    render_bundle_prompt,
)


def _bundle(query="q", files=1, chunks=0, tokens=10, relationships=None):
    hits = [
        FileHit(file_id=f"f{i}", path=f"/p/f{i}.md", summary=f"sum {i}", score=0.5, why="r")
        for i in range(files)
    ]
    chs = [
        Chunk(id=f"f0:{i}", file_id="f0", ordinal=i, text=f"chunk {i}", token_count=1)
        for i in range(chunks)
    ]
    return ContextBundle(
        query=query,
        files=hits,
        chunks=chs,
        relationships=relationships or [],
        token_estimate=tokens,
    )


class _FakeLLM:
    def __init__(self, text: str = "fake answer"):
        self.text = text
        self.last_prompt = ""

    def chat(self, prompt, *, temperature: float = 0.2):
        self.last_prompt = prompt
        return self.text


class _FakeAnthropicMessage:
    def __init__(self, text: str):
        self.content = [type("_", (), {"text": text})()]

    class _Messages:
        def __init__(self, fake):
            self._fake = fake

        def create(self, *, model, system, messages, max_tokens, **kwargs):
            self._fake.last_model = model
            self._fake.last_system = system
            self._fake.last_messages = messages
            self._fake.last_max_tokens = max_tokens
            self._fake.last_extra_body = kwargs.get("extra_body")
            return _FakeAnthropicMessage(self._fake.text)


class _FakeAnthropicClient:
    def __init__(self, text: str = "cloud answer"):
        self.text = text
        self.last_system = None
        self.last_messages = None
        self.last_model = None
        self.last_max_tokens = None
        self.last_extra_body = None
        self.messages = _FakeAnthropicMessage._Messages(self)


def test_build_synthesizer_return_only():
    s = Settings()
    s.synth.backend = "return_only"
    synth = build_synthesizer(s)
    assert isinstance(synth, ReturnOnlySynthesizer)


def test_build_synthesizer_local():
    s = Settings()
    s.synth.backend = "local"
    synth = build_synthesizer(s)
    assert isinstance(synth, LocalSynthesizer)


def test_build_synthesizer_cloud_validates_model():
    s = Settings()
    s.synth.backend = "cloud"
    s.synth.cloud.model = ""
    with pytest.raises(SynthConfigError, match="synth.cloud.model"):
        build_synthesizer(s)


def test_build_synthesizer_cloud_validates_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = Settings()
    s.synth.backend = "cloud"
    s.synth.cloud.model = "claude-sonnet-4-20250514"
    with pytest.raises(SynthConfigError, match="ANTHROPIC_API_KEY"):
        build_synthesizer(s)


def test_build_synthesizer_unknown_backend_raises():
    s = Settings()
    s.synth.backend = "nonexistent"
    with pytest.raises(ValueError):
        build_synthesizer(s)


def test_build_synthesizer_override_respected():
    s = Settings()
    s.synth.backend = "return_only"
    synth = build_synthesizer(s, mode="local")
    assert isinstance(synth, LocalSynthesizer)


def test_build_synthesizer_override_disabled_raises():
    s = Settings()
    s.synth.allow_per_call_override = False
    with pytest.raises(SynthConfigError, match="per-call mode override disabled"):
        build_synthesizer(s, mode="local")


def test_return_only_answer_returns_none_text():
    synth = ReturnOnlySynthesizer()
    bundle = _bundle(files=2)
    result = synth.answer("q", bundle)
    assert result.text is None
    assert result.mode == "return_only"
    assert result.escalated is False
    assert result.bundle is bundle


def test_local_synthesizer_calls_llm():
    fake = _FakeLLM("local response")
    s = Settings()
    synth = LocalSynthesizer(s, client=fake)
    bundle = _bundle(query="test q", files=1, chunks=2, tokens=50)
    result = synth.answer("test q", bundle)
    assert result.text == "local response"
    assert result.mode == "local"
    assert "test q" in fake.last_prompt
    assert "sum 0" in fake.last_prompt or "/p/f0.md" in fake.last_prompt


def test_cloud_synthesizer_calls_provider():
    fake = _FakeAnthropicClient("cloud response")
    s = Settings()
    s.synth.cloud.model = "claude-sonnet-4-20250514"
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        synth = CloudSynthesizer(s, client=fake)
        bundle = _bundle(query="test q", files=1)
        result = synth.answer("test q", bundle)
        assert result.text == "cloud response"
        assert result.mode == "cloud"
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)


def test_cloud_synthesizer_prompt_caching_flag():
    s = Settings()
    s.synth.cloud.model = "claude-sonnet-4-20250514"
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        # caching enabled
        s.synth.cloud.enable_prompt_caching = True
        fake = _FakeAnthropicClient("resp")
        synth = CloudSynthesizer(s, client=fake)
        bundle = _bundle(query="q", files=1)
        synth.answer("q", bundle)
        assert isinstance(fake.last_system, list)
        assert fake.last_system[0].get("cache_control") == {"type": "ephemeral"}

        # caching disabled
        s.synth.cloud.enable_prompt_caching = False
        fake2 = _FakeAnthropicClient("resp")
        synth2 = CloudSynthesizer(s, client=fake2)
        synth2.answer("q", bundle)
        assert isinstance(fake2.last_system, str)
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)


def test_render_bundle_prompt_omits_empty_sections():
    bundle = _bundle(files=0, chunks=0, tokens=0)
    prompt = render_bundle_prompt("my question", bundle)
    assert "# Question" in prompt
    assert "my question" in prompt
    assert "# Files" not in prompt
    assert "# Top chunks" not in prompt
    assert "# Relationships" not in prompt


def test_auto_local_path():
    fake_llm = _FakeLLM("local answer")
    s = Settings()
    s.synth.backend = "auto"
    local_synth = LocalSynthesizer(s, client=fake_llm)
    auto = AutoSynthesizer(s, local=local_synth)
    bundle = _bundle(files=1, tokens=10)
    result = auto.answer("small query", bundle)
    assert result.mode == "local"
    assert result.escalated is False
    assert result.text == "local answer"


def test_auto_escalate_by_files(monkeypatch):
    fake_client = _FakeAnthropicClient("cloud answer")
    s = Settings()
    s.synth.backend = "auto"
    s.synth.cloud.model = "claude-sonnet-4-20250514"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cloud_synth = CloudSynthesizer(s, client=fake_client)
    auto = AutoSynthesizer(s, cloud=cloud_synth)
    bundle = _bundle(files=5, tokens=10)
    result = auto.answer("q", bundle)
    assert result.escalated is True
    assert result.mode == "cloud"
    assert result.text == "cloud answer"


def test_auto_escalate_by_tokens(monkeypatch):
    fake_client = _FakeAnthropicClient("cloud answer")
    s = Settings()
    s.synth.backend = "auto"
    s.synth.cloud.model = "claude-sonnet-4-20250514"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cloud_synth = CloudSynthesizer(s, client=fake_client)
    auto = AutoSynthesizer(s, cloud=cloud_synth)
    bundle = _bundle(files=1, tokens=3000)
    result = auto.answer("q", bundle)
    assert result.escalated is True
    assert result.mode == "cloud"


def test_auto_escalate_by_intent(monkeypatch):
    fake_client = _FakeAnthropicClient("cloud answer")
    s = Settings()
    s.synth.backend = "auto"
    s.synth.cloud.model = "claude-sonnet-4-20250514"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cloud_synth = CloudSynthesizer(s, client=fake_client)
    auto = AutoSynthesizer(s, cloud=cloud_synth)
    bundle = _bundle(files=1, tokens=10)
    result = auto.answer("compare these documents", bundle)
    assert result.escalated is True
    assert result.mode == "cloud"


def test_auto_fallback_to_local_when_cloud_unavailable():
    fake_llm = _FakeLLM("fallback answer")
    s = Settings()
    s.synth.backend = "auto"
    s.synth.auto.fallback_to_local_if_cloud_unavailable = True
    s.synth.cloud.model = "claude-sonnet-4-20250514"
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        cloud_synth = CloudSynthesizer(s)

        def _fail(*args, **kwargs):
            raise SynthUnavailableError("cloud down")

        cloud_synth.answer = _fail
        local_synth = LocalSynthesizer(s, client=fake_llm)
        auto = AutoSynthesizer(s, local=local_synth, cloud=cloud_synth)
        bundle = _bundle(files=5, tokens=10)
        result = auto.answer("q", bundle)
        assert result.escalated is True
        assert result.mode == "local"
        assert result.text == "fallback answer"
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)


def test_auto_no_fallback_raises():
    s = Settings()
    s.synth.backend = "auto"
    s.synth.auto.fallback_to_local_if_cloud_unavailable = False
    s.synth.cloud.model = "claude-sonnet-4-20250514"
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        cloud_synth = CloudSynthesizer(s)

        def _fail(*args, **kwargs):
            raise SynthUnavailableError("cloud down")

        cloud_synth.answer = _fail
        auto = AutoSynthesizer(s, cloud=cloud_synth)
        bundle = _bundle(files=5, tokens=10)
        with pytest.raises(SynthUnavailableError):
            auto.answer("q", bundle)
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
