## 1. Owner configuration and adapter registration

- [x] 1.1 Expose `MediaProcessingConfig` through `Settings` and `katsi.toml` with no enabled executable or model by default.
- [x] 1.2 Build the fixed allowlisted adapter catalog and register only owner-configured local adapters with their declared executables, models, limits, and availability probes.
- [x] 1.3 Add configuration and registry tests for enabled adapters, absent executables, unknown bindings, and the no-config unavailable path.

## 2. Reprocessing dispatch and lifecycle

- [x] 2.1 Add an explicit `katsi index --reprocess-media` flag that preserves normal indexing behavior when omitted.
- [x] 2.2 Route current media resource versions through the configured media representation planner, compatible cache, and bounded pipeline orchestrator.
- [x] 2.3 Register each resource run's scene, keyframe, transcript, silence, and region outputs through `register_representation_batch`, preserving sibling visibility and historical incompatible fingerprints.
- [x] 2.4 Record processed, reused, unavailable, failed, and skipped outcomes; isolate a stage failure from siblings and later resources.

## 3. Verification and documentation

- [x] 3.1 Add CLI regression tests for explicit reprocessing, normal-index compatibility, cache reuse, unavailable pipelines, partial failure, and multi-scene batch persistence.
- [x] 3.2 Document the owner configuration schema, supported adapter bindings, reprocessing command, cost/availability requirements, and non-destructive behavior.
- [ ] 3.3 Run focused tests plus `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .`.
