"""Text chunking with approximate token counting.

Token estimation uses len(text) // 4 (≈4 chars per token for English-ish text).
No tokenizer dependency is used — this is a cheap heuristic.
"""

from __future__ import annotations

from mnemo_core.models import Chunk


def estimate_tokens(text: str) -> int:
    """Cheap token estimate: len(text) // 4 (≈4 chars/token for English-ish).

    Always >= 1 for non-empty text. Returns 0 for empty text.
    """
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
    """Split *text* into ~*target_tokens*-sized chunks with ~*overlap*-token overlap.

    Parameters
    ----------
    file_id:
        Stable identifier for the source file. Used as chunk id prefix.
    text:
        Source text to split.
    target_tokens:
        Approximate target size per chunk in tokens (len//4 heuristic).
    overlap:
        Approximate overlap between consecutive chunks in tokens.

    Notes
    -----
    - ``target_tokens`` and ``overlap`` are in APPROXIMATE tokens (len//4 heuristic).
    - Chunks are produced by walking through the text with a step of
      ``(target_tokens - overlap)`` in token-equivalents, then converting back to
      characters: ``step_chars = (target_tokens - overlap) * 4``.
    - On the last step, if the remaining text is shorter than *overlap* tokens,
      it is DISCARDED (no near-empty trailing chunk).
    - Each chunk's id is ``f"{file_id}:{ordinal}"`` with ordinal starting at 0.
    - ``token_count`` is ``len(chunk_text) // 4`` (always >= 1 if non-empty).
    - Text is split on character boundaries — no word-boundary awareness in v0.1.
    - If text is empty or whitespace-only, returns [].
    - If the entire text is shorter than *target_tokens*, returns ONE chunk
      containing the whole text.
    """
    if not text or not text.strip():
        return []

    step_tokens = max(1, target_tokens - overlap)
    step_chars = step_tokens * 4
    window_chars = target_tokens * 4

    if step_chars <= 0 or window_chars <= 0:
        return []

    chunks: list[Chunk] = []
    n = len(text)

    # Short text — single chunk
    if n <= window_chars:
        return [
            Chunk(
                id=f"{file_id}:0",
                file_id=file_id,
                ordinal=0,
                text=text,
                token_count=estimate_tokens(text),
            )
        ]

    overlap_chars = overlap * 4
    pos = 0
    ordinal = 0

    while pos < n:
        end = pos + window_chars
        piece = text[pos:end]
        if not piece.strip():
            break

        chunks.append(
            Chunk(
                id=f"{file_id}:{ordinal}",
                file_id=file_id,
                ordinal=ordinal,
                text=piece,
                token_count=estimate_tokens(piece),
            )
        )

        pos += step_chars
        ordinal += 1

        # If remaining text is shorter than overlap, stop — no near-empty tail.
        if n - pos < overlap_chars:
            break

    return chunks
