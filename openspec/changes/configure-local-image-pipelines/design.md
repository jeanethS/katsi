## Context

katsi never imports a vision or OCR library. Every image pipeline is an
owner-authored `MediaPipelineDefinition` naming an executable that
`BoundedSubprocessExecutor` runs with a fixed argument template, no network, a
timeout and an output-size cap. Configuring image understanding therefore means
supplying executables that satisfy strict JSON contracts — not writing pipeline
code.

Verified on the target machine before choosing anything:

| tool | state |
|---|---|
| `sips`, `magick`, `tesseract` | installed |
| `ollama` with `qwen2.5vl:7b` (vision) | installed |
| `ffmpeg` 8.1.2, `ffprobe` | installed |

## Goals / Non-Goals

**Goals**

- Produce thumbnails and OCR text for existing image resources.
- Keep every wrapper offline, deterministic in its contract, and cheap to re-run.

**Non-Goals**

- Changing any pipeline, executor or registry code.
- Visual embeddings. No local encoder on this machine emits the required
  `{embedding, space}` contract, and nothing consumes visual vectors yet.
- Captioning inside katsi. See the decision below.

## Decisions

### Thumbnail needs no wrapper; OCR does

The thumbnail contract is "write a PNG to `output_path`", which `magick`
satisfies directly through the argument template. Its default template is
`{input_path} {max_dimension} {output_path}`, so the definition supplies its own
`fixed_args` with `-auto-orient` and `-resize`.

Every other image contract is *JSON to `output_path`*, and no stock tool emits
those shapes. OCR therefore needs a wrapper translating `tesseract` output into
`{"text": ..., "regions": [...]}`.

### Captioning moves to the consumer, because the sandbox denies loopback

`_network_isolation_prefix` (`media/execution.py:117-137`) applies
`sandbox-exec -f <profile>` with `(deny network*)` on macOS when
`network_disabled=True`. Measured against the running ollama server:

```
sin sandbox:  curl localhost:11434/api/tags -> 200
con sandbox:  curl localhost:11434/api/tags -> 000
```

`(deny network*)` does not exempt loopback, so a wrapper that reaches ollama over
HTTP cannot work under katsi's isolation. Three options were considered:

1. **A truly local vision binary** (llama.cpp with a multimodal model invoked as
   a process). Respects the sandbox because no socket is involved. Viable, but
   requires standing up and maintaining a second inference stack.
2. **Declare the caption pipeline with `network_disabled=False`.** One line, and
   ollama works. Rejected: `_network_isolation_prefix` is written so that
   forfeiting isolation is a conscious act, and captioning is the pipeline most
   likely to be pointed at a remote model later.
3. **Caption in the consumer.** Rejected nothing in katsi; the consumer already
   runs outside this sandbox and already owns the closed vocabulary it would map
   captions onto.

**Chosen: option 3.** katsi supplies OCR text and scene structure; semantic
judgement about what a shot depicts belongs with whoever owns the vocabulary.
This matches the boundary already drawn for tag normalization, and leaves
katsi's "local, offline, no network" property intact.

Option 1 remains open if captions are later wanted as first-class katsi
representations.

### OCR earns its place ahead of captions

For the target library, on-screen text is the more decisive signal: distinguishing
a payment terminal from a shop front usually turns on what is written in the
frame, not on a prose description of it. OCR is also deterministic and cheap to
re-run, whereas captions are model output that must be re-validated whenever the
prompt changes.

## Risks / Trade-offs

- **Wrapper output must be valid JSON or exit non-zero.** The contract is strict:
  malformed output is a violation, never repaired. Each wrapper writes its file
  only after building the whole payload.
- **Runtime over 12 177 images.** Accepted: work is cached by fingerprint and the
  run is resumable, so a long first pass is not a blocker.
- **`tesseract` quality varies by language and by screenshot rendering.** The
  wrapper declares its language explicitly so a change of language changes the
  fingerprint and invalidates prior output.
- **No visual embeddings** means retrieval cannot rank images by visual
  similarity. Acceptable while the consumer filters on tags first.

## Migration Plan

1. Add the wrappers and their tests; neither touches katsi code.
2. Add `[katsi.media]` with the two definitions disabled by default.
3. Enable and run `--reprocess-media` over one folder; confirm representation
   counts and coverage.
4. Roll back by removing the definitions; stored representations remain as
   historical provenance and nothing is deleted.
