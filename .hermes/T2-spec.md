# T2 — Ollama clients (embed + llm)

Extends the existing katsi workspace. T0/T1 already done — do NOT recreate. Add only
the new files listed below.

## TOOL RULES (read first)

Do NOT explore any codebase. Do NOT search for anything.
Do NOT call glob, task, doom_loop, or any discovery tool.
Use ONLY your file-write/edit tool and `bash`.
Write each file directly with the exact contents specified below.

When done, run `uv run pytest -q 2>&1 | tail -20` and `uv run ruff check . 2>&1 | tail -10`
from the project root, report exit codes + tail output.

## 0. API patterns already verified (ollama 0.6.x)

```python
import ollama

client = ollama.Client(host="http://localhost:11434", timeout=120.0)

# Embeddings: pass a list of strings, get a list of vectors back.
resp = client.embed(model="bge-m3", input=["text1", "text2"])
# resp.embeddings is a list[list[float]] — len matches len(input)
vectors = resp["embeddings"]  # works either via dict access or attribute (.embeddings)

# Chat with JSON-constrained output.
resp = client.chat(
    model="qwen2.5:7b",
    messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ],
    format="json",                       # ask for valid JSON
    options={"temperature": 0.1},
)
content_str = resp.message.content       # str — the model's JSON text
```

Tests must mock the `ollama.Client` (or its methods). Use `unittest.mock` /
`pytest.MonkeyPatch`. NEVER hit real network in tests.

## 1. Existing models you can import

From `katsi_core.models`:
- `Extraction(summary: str, entities: list[dict], topics: list[str], references: list[str])`

From `katsi_core.config`:
- `Settings().ollama.host`, `Settings().ollama.embed_model`,
  `Settings().ollama.llm_model`, `Settings().ollama.timeout`

## 2. Files to create (5 new files)

```
packages/core/katsi_core/clients/__init__.py
packages/core/katsi_core/clients/embed.py
packages/core/katsi_core/clients/llm.py
tests/test_embed.py
tests/test_llm.py
```

## 3. Contract: `packages/core/katsi_core/clients/__init__.py`

```python
"""katsi Ollama clients (embeddings + LLM)."""
```

## 4. Contract: `packages/core/katsi_core/clients/embed.py`

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from katsi_core.config import Settings

if TYPE_CHECKING:
    import ollama


class EmbedClient:
    """Ollama embeddings wrapper. Reads model/host from settings."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: "ollama.Client | None" = None,
    ) -> None:
        """Hold settings; if `client` is None, lazily build ollama.Client on first
        call to embed (deferred so that construction without a running Ollama server
        is safe — important for tests + import-time)."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Batch-embed texts against Settings.ollama.embed_model.
        Returns list[list[float]] — same length as texts.
        If texts is empty, return [] (no API call).
        Uses client.embed(model=..., input=texts)."""

    @property
    def dim(self) -> int:
        """Embedding dimension. Resolved lazily by embedding a single probe text
        ('hello') and reading len(vector). Cached after first call. Tests should
        not actually depend on this hitting Ollama — inject a client whose
        .embed returns a canned embedding of known dim."""
```

Implementation note about the lazy client (apply this pattern):

```python
def _get_client(self):
    if self._client is None:
        import ollama
        self._client = ollama.Client(host=self._settings.ollama.host,
                                      timeout=self._settings.ollama.timeout)
    return self._client
```

## 5. Contract: `packages/core/katsi_core/clients/llm.py`

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from katsi_core.config import Settings
from katsi_core.models import Extraction

if TYPE_CHECKING:
    import ollama


class ExtractionError(Exception):
    """Raised when the local LLM fails to return valid Extraction JSON
    after the retry."""


SYSTEM_PROMPT = """You are a precise document analyzer.
Read the user-provided text and respond with ONE JSON object matching exactly
the shape:

{
  "summary": "string: a 1-3 sentence summary of the document",
  "entities": [{"name": "string", "kind": "string in [person, org, project]"}],
  "topics": ["string: a topic the document is about"],
  "references": ["string: any file paths, filenames, or document references this file points at"]
}

Return ONLY the JSON object. No prose, no code fences, no markdown, no
preamble, no trailing text. Output nothing else."""


class LLMClient:
    """Ollama chat client with strict-JSON extraction. Retries ONCE on a parse
    failure, then raises ExtractionError."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: "ollama.Client | None" = None,
    ) -> None:
        """Settings + optional injected client (deferred if None)."""

    def extract(self, text: str, *, attempts: int = 2) -> Extraction:
        """Single-model-call extraction of the Extraction JSON contract.

        Loop up to `attempts` times:
          1. Call _chat(SYSTEM_PROMPT, text) — temperature 0.1, format json.
          2. Parse the returned content as JSON. Tolerate: leading/trailing
             whitespace; a leading ```json or ``` fence pair surrounding the
             JSON; a trailing ``` if fence open was stripped.
          3. Validate into Extraction(**parsed). On ValidationError or
             JSONDecodeError, retry (up to attempts times).
        If all attempts fail, raise ExtractionError with a message including
        the original error + the raw model output for the final attempt."""

    def chat(self, user_text: str, *, system: str | None = None,
             temperature: float = 0.1) -> str:
        """Raw chat returning the model's message.content string."""

    def _chat(self, system_prompt: str, user_text: str) -> str:
        """Internal: invoke ollama chat with format='json' and return content string.
        Use self._settings.ollama.llm_model."""
```

Implementation reference (use this):

```python
import json
import re

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.DOTALL | re.MULTILINE)


def _clean_json(raw: str) -> str:
    """Strip markdown code fences and surrounding whitespace from raw LLM output."""
    s = raw.strip()
    # remove opening fence ```json / ``` and closing fence ```
    s = _FENCE_RE.sub("", s).strip()
    return s


class LLMClient:
    def __init__(self, settings=None, client=None):
        self._settings = settings or Settings()
        self._client = client

    def _get_client(self):
        if self._client is None:
            import ollama
            self._client = ollama.Client(
                host=self._settings.ollama.host,
                timeout=self._settings.ollama.timeout,
            )
        return self._client

    def _chat(self, system_prompt, user_text):
        resp = self._get_client().chat(
            model=self._settings.ollama.llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            format="json",
            options={"temperature": 0.1},
        )
        return resp.message.content

    def chat(self, user_text, *, system=None, temperature=0.1):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_text})
        resp = self._get_client().chat(
            model=self._settings.ollama.llm_model,
            messages=messages,
            options={"temperature": temperature},
        )
        return resp.message.content

    def extract(self, text, *, attempts=2):
        last_err = None
        last_raw = ""
        for _ in range(attempts):
            try:
                raw = self._chat(SYSTEM_PROMPT, text)
                last_raw = raw
                cleaned = _clean_json(raw)
                parsed = json.loads(cleaned)
                if isinstance(parsed, str):
                    parsed = json.loads(parsed)
                return Extraction(**parsed)
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                last_err = e
                continue
            except Exception as e:
                # pydantic ValidationError + others
                last_err = e
                continue
        raise ExtractionError(
            f"LLM did not return valid Extraction JSON after {attempts} attempts. "
            f"Last error: {last_err!r}. Last raw output: {last_raw[:400]!r}"
        )
```

Note about catching pydantic ValidationError specifically — also wrap it in the
retry loop (the import: `from pydantic import ValidationError`). Replace the
broad `except Exception` with the targeted `except (json.JSONDecodeError, TypeError, ValueError, ValidationError)`.

## 6. Contract: `tests/test_embed.py`

All tests use a FAKE ollama client. Pattern (use this):

```python
from types import SimpleNamespace
from katsi_core.clients.embed import EmbedClient


class _FakeEmbedResp:
    def __init__(self, vectors):
        self.embeddings = vectors

    def __getitem__(self, k):
        # support resp["embeddings"] style access too
        if k == "embeddings":
            return self.embeddings
        raise KeyError(k)


class _FakeOllama:
    def __init__(self, dim: int = 1024):
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, model, input):
        self.calls.append(list(input))
        return _FakeEmbedResp([[0.01 * (i + 1)] * self.dim for i in range(len(input))])


def test_embed_batches_returns_vectors():
    fake = _FakeOllama(dim=8)
    c = EmbedClient(client=fake)
    out = c.embed(["hello", "world"])
    assert len(out) == 2
    assert len(out[0]) == 8
    assert fake.calls == [["hello", "world"]]


def test_embed_empty_returns_empty():
    c = EmbedClient(client=_FakeOllama())
    assert c.embed([]) == []


def test_dim_is_cached_after_first_probe():
    fake = _FakeOllama(dim=16)
    c = EmbedClient(client=fake)
    d1 = c.dim
    d2 = c.dim
    assert d1 == 16 == d2
    # only one embed call should have happened for the probe (probe used once)
    assert len(fake.calls) == 1
```

These 3 tests minimum. Add any you find helpful.

## 7. Contract: `tests/test_llm.py`

Mock ollama by injecting a fake client. Required tests:

- `test_extract_happy_path` — fake chat returns clean JSON; extract returns Extraction.
- `test_extract_strips_fenced_json` — model wraps JSON in ```json ... ``` fences; extract
  still parses successfully.
- `test_extract_retries_once_on_bad_json` — first call returns `'not json {{'`,
  second call returns valid JSON. extract returns Extraction, fake.chat_calls length == 2.
- `test_extract_raises_after_two_failures` — both calls return junk; ExtractionError
  raised. fake.chat_calls length == 2.
- `test_extract_validates_pydantic_shape` — model returns `{"summary": "x"}` only
  (missing entities, topics, references). After retry, returns the full object.
  extract returns a valid Extraction. (Also test that ValidationError triggers retry.)
- `test_chat_passes_messages` — chat("hi") returns the content; assert the fake's
  messages structure had the user content.

Use the SimpleNamespace pattern:

```python
from types import SimpleNamespace

class _FakeChatResp:
    def __init__(self, content: str):
        self.message = SimpleNamespace(content=content)

class _FakeOllama:
    def __init__(self, replies: list[str]):
        # list of contents to return, one per .chat() call (replays in order)
        self.replies = list(replies)
        self.chat_calls: list[dict] = []

    def chat(self, model, messages, **kwargs):
        self.chat_calls.append({"model": model, "messages": messages, **kwargs})
        return _FakeChatResp(self.replies.pop(0))
```

Example for retry path:

```python
def test_extract_retries_once_on_bad_json():
    fake = _FakeOllama([ "not json {{",
        '{"summary": "ok", "entities": [], "topics": [], "references": []}' ])
    c = LLMClient(client=fake)
    e = c.extract("doc text")
    assert e.summary == "ok"
    assert len(fake.chat_calls) == 2
```

## 8. Constraints / anti-patterns

- Do NOT add new dependencies. ollama is already in katsi-core deps.
- Do NOT modify models.py, config.py, store/, mcp_server/, cli/.
- Do NOT call any external service in tests (no real Ollama).
- Do NOT leave TODO comments.
- The `dim` property must NOT make a real API call when a fake client is injected.

## 9. Done when

- All 5 files exist with the contracts above.
- `uv run pytest` passes (existing tests + at least 3 embed + 6 llm tests).
- `uv run ruff check .` is clean.
- Hand back a short report listing files created + pytest/ruff status.
