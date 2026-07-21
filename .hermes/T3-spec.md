# T3 — Extract + chunk

Extends the existing katsi workspace. T0/T1/T2 already done — add only the new files.

## TOOL RULES (read first)

Do NOT explore any codebase. Do NOT search for anything.
Use ONLY your file-write/edit tool and `bash` (for `uv run pytest`, `uv run ruff check .`).

When done run `uv run pytest -q 2>&1 | tail -20` and `uv run ruff check . 2>&1 | tail -10`
from the project root, report exit codes + tail output.

## 0. API pattern verified (markitdown 0.1.x)

```python
from markitdown import MarkItDown

md = MarkItDown()                              # no args needed
result = md.convert(str(path_or_str))          # accept Path or str path
# result.text_content: str  -- the markdown text of the file
# result.title: Optional[str]
```

`MarkItDown` constructor may raise or log if optional converters are missing. Construct
ONCE per process; reuse. On conversion failure (corrupt file, unsupported format),
`md.convert(...)` either raises or returns `text_content == ""`. Treat both as
"extraction failed: log + return empty string".

## 1. Token counting heuristic (no tokenizer dep)

For chunking, use the simple `len(text) // 4` approximation (≈4 chars per token in
English-ish text). Document this in the chunker docstring. Do NOT add `tiktoken`.
Each Chunk's `token_count` field uses this estimate.

## 2. Existing models you can import

From `katsi_core.models`:
- `Chunk(id: str, file_id: str, ordinal: int, text: str, token_count: int)`
- `FileRecord(...)` is not used in T3.

## 3. Files to create (5 new files)

```
packages/core/katsi_core/ingest/__init__.py
packages/core/katsi_core/ingest/extract.py
packages/core/katsi_core/ingest/chunk.py
tests/test_extract.py
tests/test_chunk.py
```

## 4. Contract: `packages/core/katsi_core/ingest/__init__.py`

```python
"""katsi ingest pipeline."""
```

## 5. Contract: `packages/core/katsi_core/ingest/extract.py`

```python
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Module-level singleton — MarkItDown is expensive to construct.
_MARKITDOWN: "MarkItDown | None" = None


def _get_markitdown() -> "MarkItDown":
    """Lazy singleton constructor. Imports markitdown only on first call."""
    global _MARKITDOWN
    if _MARKITDOWN is None:
        from markitdown import MarkItDown
        _MARKITDOWN = MarkItDown()
    return _MARKITDOWN


def extract_text(path: Path) -> str:
    """Extract markdown text from a file using markitdown.

    Accepts str or Path. Returns "" on:
    - file does not exist
    - file is empty
    - file extension is unsupported/[filtered]
    - markitdown raised an exception

    Always logs at INFO when starting and at WARN when extraction fails.
    Never raises out of this function — callers must get "" on failure.
    """
```

Implementation reference (use this):

```python
def extract_text(path: Path) -> str:
    p = Path(path)
    if not p.exists() or not p.is_file():
        logger.warning("extract_text: path missing or not a file: %s", p)
        return ""
    if p.stat().st_size == 0:
        logger.info("extract_text: empty file: %s", p)
        return ""
    try:
        md = _get_markitdown()
        result = md.convert(str(p))
        text = (result.text_content or "").strip()
        if not text:
            logger.info("extract_text: no text content from: %s", p)
        return text
    except Exception as e:  # pragma: no cover - exercised via mock
        logger.warning("extract_text: failed on %s: %r", p, e)
        return ""
```

Notes:
- The `except Exception` is intentional — extraction failures must NOT crash
  the ingest pipeline. Apply a `# noqa: BLE001` if ruff complains, but ruff
  config in the root pyproject selects `B` which recommends `except Exception`
  as `BLE001`. Use `except Exception` as written; ruff should pass.
- Callers will treat `""` as "skip this file" upstream.

## 6. Contract: `packages/core/katsi_core/ingest/chunk.py`

```python
from __future__ import annotations

from katsi_core.models import Chunk


def estimate_tokens(text: str) -> int:
    """Cheap token estimate: len(text) // 4 (≈4 chars/token for English-ish).
    Always >= 1 for non-empty text. Returns 0 for empty text."""


def chunk(
    file_id: str,
    text: str,
    *,
    target_tokens: int = 512,
    overlap: int = 64,
) -> list[Chunk]:
    """Split `text` into ~target_token-sized chunks with ~overlap-token overlap.

    - target_tokens and overlap are in APPROXIMATE tokens (len//4 heuristic).
    - Chunks are produced by walking through the text with a step of
      (target_tokens - overlap) in token-equivalents, then converting back to
      characters: step_chars = (target_tokens - overlap) * 4.
    - On the last step, if the remaining text is shorter than overlap_tokens,
      DISCARD it (do not produce a near-empty trailing chunk).
    - Each chunk's id is f"{file_id}:{ordinal}" with ordinal starting at 0.
    - token_count is len(chunk_text) // 4 (always >= 1 if text non-empty;
      min 1).
    - text is split on character boundaries — no need to respect word
      boundaries in v0.1.
    - If text is empty or whitespace-only, return [] (no chunks).
    - If the entire text is shorter than target_tokens, return ONE chunk
      containing the whole text.
    """
```

Implementation reference (use this):

```python
from __future__ import annotations

from katsi_core.models import Chunk


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def chunk(
    file_id: str,
    text: str,
    *,
    target_tokens: int = 512,
    overlap: int = 64,
) -> list[Chunk]:
    if not text or not text.strip():
        return []
    step_tokens = max(1, target_tokens - overlap)
    step_chars = step_tokens * 4
    window_chars = target_tokens * 4
    if step_chars <= 0 or window_chars <= 0:
        return []
    chunks: list[Chunk] = []
    pos = 0
    ordinal = 0
    n = len(text)
    if n <= window_chars:
        return [Chunk(
            id=f"{file_id}:0",
            file_id=file_id,
            ordinal=0,
            text=text,
            token_count=estimate_tokens(text),
        )]
    overlap_chars = overlap * 4
    while pos < n:
        end = pos + window_chars
        piece = text[pos:end]
        if not piece.strip():
            break
        chunks.append(Chunk(
            id=f"{file_id}:{ordinal}",
            file_id=file_id,
            ordinal=ordinal,
            text=piece,
            token_count=estimate_tokens(piece),
        ))
        # advance by step_chars; if remaining text is shorter than overlap,
        # stop (don't emit a near-empty trailing chunk).
        pos += step_chars
        if n - pos < overlap_chars:
            break
        ordinal += 1
    return chunks
```

Follow the contract above. The reference is a guide — you may improve edge cases
but DO stay strictly within the contract: deterministic ordinals/ids, ~target_tokens
size, ~overlap overlap, near-empty trailing chunk suppressed.

## 7. Contract: `tests/test_extract.py`

Use `tmp_path` to create fixture files. Coverage (minimum 5 tests):

- `test_extract_markdown_file` — write a small `.md` file, verify extract_text returns non-empty markdown.
- `test_extract_python_file` — write a small `.py` file with a function, verify extract_text returns something (Python source may be returned as text with code fences — assert it contains a function name).
- `test_extract_missing_file_returns_empty` — path that doesn't exist → "".
- `test_extract_empty_file_returns_empty` — write a 0-byte file → "".
- `test_extract_failure_returns_empty` — monkeypatch `_get_markitdown` (or `MarkItDown.convert`) to raise → "" returned, no exception raised.

Use `tmp_path / "x.md"` etc. for fixtures.

## 8. Contract: `tests/test_chunk.py`

Coverage (minimum 6 tests):

- `test_chunk_empty_returns_empty_list` — chunk("f", "", ...) → [].
- `test_chunk_whitespace_only_returns_empty_list` — chunk("f", "   \n ", ...) → [].
- `test_chunk_small_text_single_chunk` — text shorter than target → one chunk
  with the whole text, ordinal 0, id "f:0".
- `test_chunk_long_text_multiple_chunks` — text ~4*target_tokens chars →
  multiple chunks; verify ordinals are 0,1,2,...; ids are f:0, f:1, f:2;
  each token_count <= target_tokens+slack; consecutive chunks overlap by ~overlap_tokens.
  Use a 6000-char text with target_tokens=512, overlap=64:
  expect len(chunks) >= 4 (verify type).
- `test_chunk_ids_and_ordinals_are_deterministic` — two calls with same args
  produce identical list of Chunk.
- `test_chunk_token_count_positive` — every emitted chunk has token_count >= 1.
- `test_estimate_tokens_basic` — estimate_tokens("") == 0; estimate_tokens("hello") == 1;
  estimate_tokens("x" * 100) == 25.

## 9. Constraints

- Do NOT add new dependencies. markitdown is already in katsi-core deps.
- Do NOT modify models.py, config.py, store/, clients/, mcp_server/, cli/.
- Do NOT leave TODO comments.
- Do NOT try to install optional converter dependencies (e.g. pdfminer) — rely
  on what's bundled with the default markitdown install. The tests above use
  .md/.txt/.py which the default install handles.

## 10. Done when

- All 5 files exist with the contracts above.
- `uv run pytest` passes (existing tests + 5 extract + 7 chunk = expect ~43+).
- `uv run ruff check .` is clean.
- Hand back a short report listing files created + pytest/ruff status.
