# Multimedia understanding

Multimedia support is local-first and disabled unless an owner configures and
registers an available pipeline. Text-only installs continue to work without
OCR, transcription, captioning, or video dependencies.

## Configuration and dependencies

Install only the local adapter dependencies selected by the owner, then use
their availability probe before enabling a media include pattern. Adapter names,
models, timeouts, memory limits, output limits, and sampling policy belong in
the owner-controlled pipeline catalog; agents must never supply commands or
model names. `MediaSamplingSettings` fingerprints chunk target, overlap, and
separator policy, so policy changes create new representations rather than
silently reusing incompatible cached output.

Keep media pipelines local. Remote upload, face identity, voice identity, and
emotion inference are not initial capabilities.

## Reprocess existing media

Normal `katsi index PATH` does not run semantic media work. To process current,
already tracked resources without deleting their source, text index, or prior
representations, use:

```bash
uv run katsi start /path/to/workspace          # tracks the files
uv run katsi index --reprocess-media /path/to/workspace
```

`--reprocess-media` only sees resources the workspace already tracks, and
reconciliation is what tracks them, so run `katsi start` on the root first.
Image, audio, and video extensions are in the default `ingest.include_globs`
for that reason; text indexing reports them as `skipped`, never as errors,
because their content belongs to the media pipelines below.

Add `[katsi.media]` with the relevant family enabled and owner-authored
`[[katsi.media.pipelines]]` entries. Each entry needs an `adapter_binding`,
its executable path, fixed arguments, limits, and optional availability probe.
The currently supported bindings are `video_metadata_ffprobe`,
`video_scene_detect_ffmpeg`, `audio_decode_ffmpeg`, `image_thumbnail_magick`,
and `image_ocr_tesseract`. Silence detection, transcription, regions, captions,
and keyframes need their upstream derived artifacts and are unavailable until
their complete pipeline is configured. No executable, model, or wrapper is
selected or downloaded by Katsi; unsupported or absent adapters are reported
unavailable.

Reprocessing reuses compatible content/fingerprint results. Changed executable
policy or sampling produces a new historical generation; it never removes the
original file or old representation.

## Local image pipelines (thumbnails and OCR)

Image understanding is owner-configured and disabled unless two things exist:
the `[katsi.media]` block below and the local tools it names. Nothing is
selected, downloaded, or guessed. By default everything stays off:

```toml
[katsi.media]
enable_image_processing = false  # set true only after the executables below exist

# Thumbnail: ImageMagick writes the PNG directly; no wrapper is needed because
# the thumbnail contract is a file, not JSON. `-auto-orient` applies EXIF
# orientation before resizing; `512x512>` only shrinks larger images.
[[katsi.media.pipelines]]
id = "image_thumbnail_v1"
adapter_binding = "image_thumbnail_magick"
name = "Orientation-normalized thumbnail (magick)"
stage = "generate_thumbnail"
accepted_mime_patterns = ["image/*"]
representation_kinds_produced = ["thumbnail"]
producer_type = "deterministic"
executable_path = "/opt/homebrew/bin/magick"
fixed_args = ["{input_path}", "-auto-orient", "-resize", "512x512>", "{output_path}"]
network_disabled = true
timeout_seconds = 30

# OCR: tesseract cannot emit the katsi JSON contract itself, so the
# owner-supplied wrapper at tools/media/ocr_tesseract.py translates TSV into
# {"text": ..., "regions": [{text, bbox, confidence}]}. The language is an
# explicit argument on purpose: it is part of fixed_args, so changing it
# changes the pipeline fingerprint and invalidates cached OCR instead of
# silently reusing text produced under another language.
[[katsi.media.pipelines]]
id = "image_ocr_v1"
adapter_binding = "image_ocr_tesseract"
name = "Local image OCR (tesseract wrapper)"
stage = "ocr"
accepted_mime_patterns = ["image/*"]
representation_kinds_produced = ["ocr_text"]
producer_type = "deterministic"
executable_path = "/Users/jeanhrdz/katsi/tools/media/ocr_tesseract.py"
fixed_args = ["{input_path}", "{output_path}", "--lang", "spa+eng"]
network_disabled = true
timeout_seconds = 60
```

Both pipelines run under `sandbox-exec` with `(deny network*)`; the wrapper
opens no sockets and reaches nothing on loopback. That constraint is why
captioning is deliberately absent from this configuration: a caption pipeline
would need to reach a vision model over HTTP, which the sandbox denies by
design. Semantic description of what an image depicts belongs to the consumer,
outside katsi, derived from the OCR text and scene evidence recorded here.
Visual embeddings are likewise unconfigured: no local encoder emits the
required `{embedding, space}` contract, so retrieval cannot rank by visual
similarity until one is supplied.

The `image_thumbnail_magick` and `image_ocr_tesseract` bindings are validated
at registration against each definition's declared stage, produced kinds, and
accepted MIME patterns — a mismatch fails registration loudly rather than
surfacing at run time.

## Privacy

Original bytes and derived blobs are private. OCR, captions, transcripts,
filenames, subtitles, and metadata are untrusted evidence, not instructions.
Location and biometric-like metadata stay out of normal search/context surfaces
unless a matching capability grant permits them. Do not put raw media, base64,
full-resolution images, or complete transcripts in a default context bundle.

## MCP result contract

A media result is citation-first: resource version, representation id/kind and
status, coverage, producer provenance, relevance contributions, typed locator,
and a bounded text preview or thumbnail reference. Locators are normalized image
regions, one-based PDF pages (with optional region), millisecond audio ranges,
or video frame timestamps. Clients retrieve a cited preview or original through
a separate capability-checked operation.

## Troubleshooting

- An unavailable representation means the detected media is preserved but no
  configured local adapter accepted it; inspect the detector warning and probe.
- A partial representation is usable only for its recorded coverage; raise the
  relevant budget before treating the whole resource as understood.
- A cache miss after changing a model, prompt, language, or sampling policy is
  expected because those values are part of the pipeline fingerprint.
- If a result lacks visual search, retain text-derived OCR/caption/transcript
  retrieval; compatible visual embeddings must not be mixed with text vectors.
