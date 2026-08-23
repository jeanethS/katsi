## 1. Wrappers

- [x] 1.1 Add `tools/media/ocr_tesseract.py`: read `argv[1]`, run `tesseract` with an explicit language, and write `{"text": str, "regions": [{"text", "bbox", "confidence"}]}` to `argv[2]`. Build the whole payload before writing; exit non-zero on any failure rather than emitting partial JSON.
- [x] 1.2 Derive `regions` from tesseract TSV output, converting pixel boxes to normalized `[x, y, w, h]` against the image dimensions. Omit `regions` entirely when confidence data is unavailable, rather than emitting empty or guessed boxes.
- [x] 1.3 Add unit tests over captured tesseract output covering: text-only result, text with regions, an image with no detectable text (valid empty `text`, not a failure), and malformed tesseract output (non-zero exit, no file written).

## 2. Configuration

- [x] 2.1 Add a documented `[katsi.media]` block for `~/.katsi/katsi.toml` with `enable_image_processing = false` by default, and `[[katsi.media.pipelines]]` entries for the thumbnail and OCR definitions including `adapter_binding`, `executable_path`, `fixed_args` and timeouts.
- [x] 2.2 Bind the thumbnail definition to `magick` with `-auto-orient` and `-resize`, writing PNG to `{output_path}`; no wrapper.
- [x] 2.3 Verify each definition's declared `stage`, `representation_kinds_produced` and `accepted_mime_patterns` match its adapter binding, so registration fails loudly on a mismatch rather than at run time.
- [x] 2.4 Confirm both pipelines declare `network_disabled = true` and that neither wrapper opens a socket.

## 3. Verification

- [ ] 3.1 Run the configured pipelines over a small folder and confirm representation counts, coverage fractions and `is_current` visibility for both kinds.
- [ ] 3.2 Re-run unchanged and confirm every representation is reused and no executable is invoked.
- [ ] 3.3 Change the OCR language, re-run, and confirm a new current representation is created while the prior one is retained as history.
- [ ] 3.4 Point one definition at a nonexistent executable and confirm it is reported unavailable without aborting the run.
- [x] 3.5 Run `uv run pytest`, `uv run ruff check .` and `uv run ruff format --check .`.

## 4. Handover

- [x] 4.1 Document in the consumer's design that semantic description is derived consumer-side from katsi's OCR and scene evidence, recording the measured loopback constraint as the reason.
- [x] 4.2 Note that visual embeddings remain unconfigured, so retrieval cannot rank by visual similarity.

## Dependencies

- `reprocess-media-representations` provides the execution path. Until it runs configured pipelines and expands aggregate results into per-item representations, these definitions have nothing to execute.
