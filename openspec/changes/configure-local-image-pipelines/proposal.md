## Why

The media layer registers 19 248 resources in the `/Users/jeanhrdz/Documents/media`
workspace, and every one of them carries a single representation kind:
`media_descriptor`. No thumbnails, no OCR, no captions.

That is not a defect in the pipelines. `build_thumbnail_pipeline_definition`,
`build_ocr_pipeline_definition`, `build_caption_pipeline_definition` and
`build_embedding_pipeline_definition` all default to `executable_path=None`
deliberately, so `check_availability` reports unavailable rather than guessing at
a system tool. Nobody has ever supplied the executables.

Until they exist, image resources cannot contribute text to the index and cannot
be filtered by anything a consumer could trust.

## What Changes

- Add two owner-supplied local wrappers — thumbnail and OCR — that satisfy the
  existing strict subprocess contracts.
- Document the `[katsi.media]` configuration that binds them, so
  `katsi index --reprocess-media` has something to execute.
- Record the measured constraint that stops captioning from living here, and
  move that responsibility to the consumer rather than weakening katsi's
  isolation guarantee.

## Impact

- Affected specs: `local-image-understanding` (new).
- Affected code: no changes to `katsi_core.media` pipeline code. This change adds
  wrapper scripts and configuration only.
- Depends on `reprocess-media-representations` for the execution path.
