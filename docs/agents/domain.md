# Domain Docs

Katsi uses a multi-context domain-documentation layout.

## Before exploring

Read `CONTEXT-MAP.md` at the repository root, then read each linked `CONTEXT.md` relevant to the work.

Also read applicable ADRs from:

- `docs/adr/` for system-wide decisions
- Context-specific `docs/adr/` directories for local decisions

Missing domain files are created lazily through `/domain-modeling`; their absence is not an error.

## Context layout

The intended contexts are:

- `packages/core/`
- `packages/mcp_server/`
- `packages/cli/`
- `packages/app/`

`CONTEXT-MAP.md` will point to the relevant context documents as they are created.

## Vocabulary

Use terminology defined in the context glossaries. Do not replace established terms with synonyms.

If a needed concept is absent, reconsider the terminology or record the gap for `/domain-modeling`.

## ADR conflicts

Explicitly flag any proposal that contradicts an existing architectural decision instead of silently overriding it.
