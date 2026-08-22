## Why

Existing media can have only a metadata descriptor because the multimedia
configuration and pipeline registry are not connected to `Settings` or the
CLI. Re-running `katsi index` must be able to generate missing semantic
representations without deleting the workspace index.

## What Changes

- Surface owner-controlled media configuration in `katsi.toml`, including
  enabled families and executable/model settings for registered adapters.
- Build the production registration path from that configuration to a fixed
  allowlist of local adapter bindings; katsí ships no executable paths.
- Add `katsi index --reprocess-media` for tracked media.
- Reuse compatible successful representations and persist sibling outputs as a
  batch so one scene/keyframe/transcript/silence result cannot hide another.

## Capabilities

### New Capabilities

- `media-reprocessing`: Safely regenerate missing or incompatible derived media
  representations for existing workspace resources.

### Modified Capabilities

- None.

## Impact

- Affects configuration loading, adapter registration, `katsi index` dispatch,
  and media representation persistence.
- Adds CLI and integration tests using fake local media pipelines; no original
  bytes, workspace history, or text index records are deleted.
