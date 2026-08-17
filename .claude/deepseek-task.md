# Task: recursive chunker for katsi

## Repository purpose

katsi is a local-first relational file-context engine. An ingest pipeline
extracts text from files (markitdown), splits it into chunks, embeds the chunks
(Ollama), and projects them into a LanceDB vector store plus a Kuzu graph.
Retrieval fuses vector similarity with shared entities/topics.

## The unit of work

Replace the naive fixed-character-window chunker with **recursive separator
splitting**, and change the size heuristic to non-whitespace density.

This is the current implementation, and it is the only thing you are changing.

### Required behaviour

1. `estimate_tokens(text)` becomes non-whitespace density:
   `sum(1 for c in text if not c.isspace()) // 3`, floored at 1 for non-empty
   text, 0 for empty text. Rationale: the previous `len(text) // 4` counted
   indentation and blank lines as content, inflating the estimate for code and
   padded text (cAST, arXiv 2506.15655).

2. `chunk(file_id, text, *, target_tokens=512, overlap=64)` splits recursively
   using the separator hierarchy, in this exact priority order:

   ```python
   ["\n\n", "\n", ". ", " ", ""]
   ```

   Algorithm:
   - If `estimate_tokens(text) <= target_tokens`, emit the text as one piece.
   - Otherwise pick the first separator in the list that occurs in the text,
     split on it, then greedily merge adjacent pieces into groups that stay
     within `target_tokens`.
   - Any single piece still over `target_tokens` recurses using only the
     REMAINING separators (never re-tries a separator already used).
   - The terminal `""` separator performs a hard character slice. It exists to
     guarantee termination on pathological input such as one 10,000-character
     word with no separators at all.

3. Overlap is applied as a POST-PASS over the assembled pieces: prepend the
   trailing `overlap` tokens' worth of text from piece N to piece N+1. Do not
   try to weave overlap into the recursion.

4. Chunk identity is unchanged: `id = f"{file_id}:{ordinal}"`, `ordinal`
   sequential from 0, `token_count = estimate_tokens(chunk_text)`.

## Contracts to preserve — do not change these

- The signature `chunk(file_id, text, *, target_tokens=512, overlap=64)
  -> list[Chunk]`. The pipeline calls it by keyword at
  `packages/core/katsi_core/ingest/pipeline.py:161`.
- The `Chunk` pydantic model in `packages/core/katsi_core/models.py`. It has
  exactly `id, file_id, ordinal, text, token_count`. Do NOT add a metadata
  field; the LanceDB Arrow schema is pinned separately and would break.
- Empty or whitespace-only input returns `[]`.
- Text that fits in one chunk returns exactly ONE chunk containing the whole
  text, unmodified.
- `estimate_tokens("")` returns 0.
- Module must remain dependency-free: standard library only. No tiktoken, no
  transformers, no langchain.

## Bug to fix as part of this

The current loop contains:

```python
piece = text[pos:end]
if not piece.strip():
    break
```

A whitespace-only window in the MIDDLE of a document terminates chunking early
and silently discards the remainder. The new implementation must not lose
content. This is the single most important acceptance check.

## Allowed paths

- `packages/core/katsi_core/ingest/chunk.py` (rewrite)
- `tests/test_chunk.py` (extend; you may update
  `test_estimate_tokens_basic`, whose expected values change deliberately
  because the heuristic changed)

## Exclusions — do not touch

- `pipeline.py`, `models.py`, `config.py`, `vectors.py`, `graph.py`
- Anything under `packages/core/katsi_core/workspace/`
- Any file outside the two allowed paths above

## Acceptance checks

Write these as pytest tests in `tests/test_chunk.py`, in addition to keeping
the 7 existing tests passing:

1. **No content loss (most important).** For a document containing a run of
   >2048 whitespace characters in the middle, the concatenation of all chunk
   texts, with overlap regions removed, reconstructs the original text. This
   test must fail against the OLD implementation and pass against the new one.
2. **Prefers paragraph boundaries.** Text made of paragraphs separated by
   `\n\n`, sized so several fit per chunk, splits on `\n\n` and never mid-word.
3. **Size bound.** No emitted chunk exceeds `target_tokens`, except a chunk
   consisting of a single unbreakable atom (a word longer than the target).
4. **Termination.** A 10,000-character string with no whitespace at all
   terminates and produces chunks.
5. **Determinism.** Calling `chunk()` twice on identical input yields
   identical ids, ordinals, and texts.
6. Existing behaviour: empty input, whitespace-only input, short single-chunk
   input, sequential ordinals.

Run: `uv run pytest tests/test_chunk.py -q`

## Response format

Return a unified diff for each changed file, followed by a short plain-text
explanation of the recursion and how the overlap post-pass avoids
double-counting text. Do not include commentary inside the diffs.
