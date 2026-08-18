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
