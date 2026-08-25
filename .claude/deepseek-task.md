> **You have NO tools and NO filesystem access.** Do not emit tool calls, do not
> try to read files, and do not ask to inspect the repository. Everything you
> need is inline in this packet. Respond with code and notes only.

# Task packet: asset-bridge tagging and ShotRef store

## Repository purpose

`asset-bridge` is a Brand OS microservice (its own git repo, sibling of the
others). It lets Brand OS reuse footage it already owns by translating between a
Redis bus and katsí's MCP tools. katsí is a separate local-first knowledge system
that indexes media into scenes, keyframes, transcripts and silence spans.

You are implementing two units. Both are pure logic with injected dependencies —
no Redis, no katsí, no network.

## Stack

Node >= 20, `"type": "module"`, TypeScript 5.4 strict, vitest.
**Imports use explicit `.js` extensions** (ESM), matching sibling services.

## Exact facts (verified — do not deviate, do not invent)

`@brand-os/contracts` is an existing package consumed as
`file:../infra-social/contracts`. It exports, among others:

```ts
// Closed vocabulary (src/video.ts) -- const arrays, use (typeof X)[number] for the type
export const LUGAR = ['beijing','xian','chengdu','zhangjiajie','chongqing',
  'guilin_yangshuo','guangzhou','hong_kong','shanghai','cdmx','otro'] as const;
export const TEMA = ['comida','hotel','tren','pago_qr','compras','calle','paisaje',
  'evento','escenario','codigo','pantalla','reunion','producto','persona',
  'transporte','cotidiano'] as const;
export const ENCUADRE = ['primer_plano','medio','general','cenital','pov','screen_recording'] as const;
export const CALIDAD_VISUAL = ['alta','media','baja'] as const;
export const APTO_VERTICAL = ['si','no','recortable'] as const;
```

```ts
// src/schemas/video.zod.ts
export const ShotTagsSchema = z.object({
  lugar: z.enum(LUGAR).nullable(),
  tema: z.array(z.enum(TEMA)).min(1).max(3),
  encuadre: z.enum(ENCUADRE).nullable(),
  presencia_jeaneth: z.boolean().nullable(),
  calidad_visual: z.enum(CALIDAD_VISUAL).nullable(),
  apto_vertical: z.enum(APTO_VERTICAL).nullable(),
});

export const ShotRefSchema = z.object({
  shot_id: z.string(),
  katsi_workspace_id: z.string(),
  representation_id: z.string(),
  locator: z.string(),
  source_path: z.string().min(1).optional(),
  tags: ShotTagsSchema,
  duracion_s: z.number().nonnegative(),
});

export type ShotRef = z.infer<typeof ShotRefSchema>;
export type ShotTags = z.infer<typeof ShotTagsSchema>;
```

`ShotTags` and `ShotRef` types are importable from `@brand-os/contracts`.

A sibling service already establishes the house pattern of injecting a
*structural* storage interface rather than a client: `AssetUsageLedger`'s
constructor takes a `SortedSetStore` with `zadd`/`zcount`, not ioredis. Follow
that.

The Redis ledger key prefix used by that sibling is exactly:
`brand-os:asset-uses:${shotId}`

## Unit A — `src/tagging/paths.ts` and `src/tagging/normalize.ts`

This decides whether the whole service is useful: tagging quality caps retrieval
quality, and no amount of ranking repairs a wrong tag.

### `paths.ts`

```ts
export function lugarFromPath(path: string): Lugar | null
```

Derive `lugar` from folder structure — `/media/01_CHINA/beijing/clip.mp4` →
`'beijing'`. Folder names encode location far more reliably than any caption, so
a caption that disagrees is treated as the caption being wrong. Case-insensitive.
Must match multi-word entries like `guilin_yangshuo`. Returns `null` rather than
guessing; `null` is a real answer.

### `normalize.ts`

```ts
export interface ShotEvidence {
  caption: string | null;
  ocrText: string | null;
  transcript: string | null;
  path: string;
  aspectRatio: number | null;
}

export interface TemaClassifier {
  classify(evidence: ShotEvidence): Promise<{ tema: Tema[]; confidence: number }>;
}

export async function normalizeTags(
  evidence: ShotEvidence,
  classifier: TemaClassifier,
  opts?: { minConfidence?: number },   // default 0.4
): Promise<ShotTags | null>
```

Rules, all load-bearing:

- The classifier is **injected** so this is testable with no model and the real
  one can be swapped without touching these rules. Do not implement a keyword
  table, and do not call any model.
- Returns `null` when the shot cannot be placed: no valid `tema`, or confidence
  below `minConfidence`. **An untagged shot is invisible to query, which is
  correct** — the downstream filter treats tags as a guarantee, so a wrong tag is
  worse than a missing one.
- Discard any `tema` value not in the `TEMA` vocabulary, then cap at 3
  (`ShotTagsSchema` enforces `.min(1).max(3)`).
- `lugar` comes from `lugarFromPath`, never from the caption.
- `apto_vertical` is **geometry, never language**: derive from `aspectRatio`.
  `<= 1` → `'si'`; `> 1` → `'recortable'`; `null` → `null`. A caption cannot tell
  you a frame's shape.
- `encuadre`, `presencia_jeaneth`, `calidad_visual` stay `null`. `null` is a real
  answer; do not guess.

## Unit B — `src/store/shot-refs.ts` and `src/store/remap.ts`

### `shot-refs.ts`

```ts
export interface ShotRefStore {
  upsertMany(refs: ShotRef[]): Promise<void>;
  byPath(path: string): Promise<ShotRef[]>;
  all(): Promise<ShotRef[]>;
}

export class InMemoryShotRefStore implements ShotRefStore { ... }
```

`upsertMany` keys on `shot_id` (later writes replace earlier ones). `byPath`
matches `ref.source_path === path`.

### `remap.ts` — the highest-risk unit here

```ts
export interface LedgerRemapStore {
  rename(oldKey: string, newKey: string): Promise<void>;
}

export function planRemap(previous: ShotRef[], next: ShotRef[]): Array<{ from: string; to: string }>

export async function applyRemap(
  plan: Array<{ from: string; to: string }>,
  store: LedgerRemapStore,
): Promise<void>
```

**Why this exists.** `shot_id` is katsí's `representation_id`, which belongs to a
representation *generation*. Re-running scene detection mints new ids for the
same footage. The usage ledger is keyed on `shot_id`, so without a remap a
re-harvest silently forgets everything already posted and the system goes back to
serving the same five shots — the exact failure the ledger exists to prevent. It
fails **invisibly**, so its tests matter most.

- Identity across generations is `(source_path, locator)` — file plus scene
  boundaries — not the id.
- Emit a pair only when a match is found **and** the id actually changed.
- `applyRemap` calls `store.rename('brand-os:asset-uses:' + from, 'brand-os:asset-uses:' + to)`
  for each pair, preserving every `used_at` score.

## Required tests

Write `src/tagging/paths.test.ts`, `src/tagging/normalize.test.ts`, and
`src/store/remap.test.ts` with vitest (`import { describe, expect, it } from 'vitest'`).

Cover at minimum:

**paths:** known city from a segment; multi-word entry `guilin_yangshuo`;
case-insensitive (`Shanghai`); unknown path → `null`.

**normalize:** `lugar` comes from the path even when the caption names a
different city; no valid `tema` → `null`; confidence below floor → `null`;
four themes capped to three; a `tema` value outside the vocabulary is discarded;
`aspectRatio` 16/9 → `'recortable'`, 9/16 → `'si'`, `null` → `null`; `encuadre`
and `presencia_jeaneth` stay `null`.

**remap:** boundaries match → pair emitted; different boundaries → no pair;
different `source_path` → no pair; unchanged id → no pair; multi-scene generation
maps every member; `applyRemap` calls `rename` with the exact
`brand-os:asset-uses:` prefixed keys.

## Exclusions

- No new dependencies. No Redis, no MCP, no model calls, no filesystem access.
- Do not redefine any schema locally — import types from `@brand-os/contracts`.
- No beat detection, tempo, energy analysis, ffmpeg, or EDL logic. This service
  answers *which shot and which milliseconds* only.
- Do not invent exports beyond those specified.

## Acceptance checks

I will run these myself:

```
npx vitest run
npm run build
```

## Response format

Two sections.

### Code

One plain ```ts block per file, each preceded by its path.

### Notes

- Anything in this packet that did not fit, or that you could not verify.
- Anything you were unsure about. Do not invent problems to look thorough.
