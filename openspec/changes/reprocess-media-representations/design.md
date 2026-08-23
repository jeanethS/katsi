## Context

`katsi index` only reconciles an existing media resource into a metadata
descriptor. `MediaProcessingConfig` and `MediaPipelineRegistry` are not wired
into `Settings` or the CLI, so no production caller registers an adapter. The
registry already supports immutable history, compatible reuse, and batch
insertion.

## Goals / Non-Goals

**Goals:**

- Add `katsi index --reprocess-media PATH`.
- Expose an owner-controlled media catalog in `Settings` and register its
  local adapters once at CLI startup.
- Reuse compatible work and summarize outcomes.

**Non-Goals:**

- Delete source, derived, workspace, or text-index state.
- Change normal indexing or bypass configured limits.
- Introduce remote media services or a second command.

## Decisions

### Make reprocessing an explicit index flag

The flag is opt-in because semantic media stages can be expensive while it
keeps traversal and workspace lookup unchanged.

### Bind configuration to an allowlisted adapter catalog

`Settings` exposes `MediaProcessingConfig`. A registry builder maps
owner-authored pipeline definitions to a fixed set of local adapter classes.
Executable paths, models, limits, and enablement remain owner supplied in
`katsi.toml`; no dynamic imports or implicit tool selection are allowed.

### Process current resource versions through one dispatcher

The dispatcher plans configured stages, checks the compatible cache, runs only
misses through the bounded orchestrator, and records outcomes independently.

### Register related outputs in a batch

Each resource run persists its scene/keyframe/transcript/silence/region outputs
with the registry batch operation so same-kind siblings remain visible.

## Risks / Trade-offs

- [Large libraries take time] → explicit command with existing bounds.
- [Missing executable] → unavailable result; never substitute a tool.
- [Policy change creates data] → fingerprints retain history.
