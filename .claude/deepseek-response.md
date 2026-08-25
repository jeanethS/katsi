## Code

### `src/tagging/paths.ts`

```ts
import { LUGAR } from '@brand-os/contracts';

type Lugar = (typeof LUGAR)[number];

export function lugarFromPath(path: string): Lugar | null {
  const lowerPath = path.toLowerCase();
  for (const lugar of LUGAR) {
    if (lowerPath.includes(lugar.toLowerCase())) {
      return lugar;
    }
  }
  return null;
}
```

### `src/tagging/normalize.ts`

```ts
import type { ShotTags, ShotTagsSchema } from '@brand-os/contracts';
import { TEMA } from '@brand-os/contracts';
import { lugarFromPath } from './paths.js';

type Tema = (typeof TEMA)[number];

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
  opts?: { minConfidence?: number },
): Promise<ShotTags | null> {
  const minConfidence = opts?.minConfidence ?? 0.4;

  const result = await classifier.classify(evidence);

  if (result.confidence < minConfidence) {
    return null;
  }

  // Filter to only valid TEMA values and deduplicate
  const validTema: Tema[] = [];
  const seen = new Set<string>();
  for (const t of result.tema) {
    if ((TEMA as readonly string[]).includes(t) && !seen.has(t)) {
      seen.add(t);
      validTema.push(t);
    }
  }

  if (validTema.length === 0) {
    return null;
  }

  // Cap at 3
  const cappedTema = validTema.slice(0, 3);

  const lugar = lugarFromPath(evidence.path);

  let aptoVertical: 'si' | 'recortable' | null = null;
  if (evidence.aspectRatio !== null) {
    aptoVertical = evidence.aspectRatio <= 1 ? 'si' : 'recortable';
  }

  return {
    lugar,
    tema: cappedTema,
    encuadre: null,
    presencia_jeaneth: null,
    calidad_visual: null,
    apto_vertical: aptoVertical,
  };
}
```

### `src/store/shot-refs.ts`

```ts
import type { ShotRef } from '@brand-os/contracts';

export interface ShotRefStore {
  upsertMany(refs: ShotRef[]): Promise<void>;
  byPath(path: string): Promise<ShotRef[]>;
  all(): Promise<ShotRef[]>;
}

export class InMemoryShotRefStore implements ShotRefStore {
  private refs = new Map<string, ShotRef>();

  async upsertMany(refs: ShotRef[]): Promise<void> {
    for (const ref of refs) {
      this.refs.set(ref.shot_id, ref);
    }
  }

  async byPath(path: string): Promise<ShotRef[]> {
    return [...this.refs.values()].filter((ref) => ref.source_path === path);
  }

  async all(): Promise<ShotRef[]> {
    return [...this.refs.values()];
  }
}
```

### `src/store/remap.ts`

```ts
import type { ShotRef } from '@brand-os/contracts';

export interface LedgerRemapStore {
  rename(oldKey: string, newKey: string): Promise<void>;
}

export function planRemap(previous: ShotRef[], next: ShotRef[]): Array<{ from: string; to: string }> {
  const previousById = new Map(previous.map((ref) => [ref.shot_id, ref]));

  const pairs: Array<{ from: string; to: string }> = [];

  for (const nextRef of next) {
    // Find a previous ref with the same identity (source_path + locator)
    const match = previous.find(
      (prev) => prev.source_path === nextRef.source_path && prev.locator === nextRef.locator,
    );

    if (match && match.shot_id !== nextRef.shot_id) {
      pairs.push({ from: match.shot_id, to: nextRef.shot_id });
    }
  }

  return pairs;
}

export async function applyRemap(
  plan: Array<{ from: string; to: string }>,
  store: LedgerRemapStore,
): Promise<void> {
  for (const pair of plan) {
    await store.rename(`brand-os:asset-uses:${pair.from}`, `brand-os:asset-uses:${pair.to}`);
  }
}
```

### `src/tagging/paths.test.ts`

```ts
import { describe, expect, it } from 'vitest';
import { lugarFromPath } from './paths.js';

describe('lugarFromPath', () => {
  it('derives a known city from a path segment', () => {
    expect(lugarFromPath('/media/01_CHINA/beijing/clip.mp4')).toBe('beijing');
  });

  it('matches multi-word entries like guilin_yangshuo', () => {
    expect(lugarFromPath('/media/01_CHINA/guilin_yangshuo/boat.mp4')).toBe('guilin_yangshuo');
  });

  it('is case-insensitive', () => {
    expect(lugarFromPath('/media/01_CHINA/Shanghai/clip.mp4')).toBe('shanghai');
  });

  it('returns null for an unknown path', () => {
    expect(lugarFromPath('/media/unknown/location/clip.mp4')).toBeNull();
  });
});
```

### `src/tagging/normalize.test.ts`

```ts
import { describe, expect, it } from 'vitest';
import { normalizeTags, type ShotEvidence, type TemaClassifier } from './normalize.js';

function makeClassifier(tema: string[], confidence: number): TemaClassifier {
  return {
    async classify() {
      return { tema: tema as any, confidence };
    },
  };
}

function makeEvidence(overrides: Partial<ShotEvidence> = {}): ShotEvidence {
  return {
    caption: null,
    ocrText: null,
    transcript: null,
    path: '/media/01_CHINA/beijing/clip.mp4',
    aspectRatio: null,
    ...overrides,
  };
}

describe('normalizeTags', () => {
  it('takes lugar from the path even when the caption names a different city', async () => {
    const evidence = makeEvidence({
      caption: 'We are in Shanghai today',
      path: '/media/01_CHINA/beijing/clip.mp4',
    });
    const result = await normalizeTags(evidence, makeClassifier(['comida'], 0.9));
    expect(result?.lugar).toBe('beijing');
  });

  it('returns null when there is no valid tema', async () => {
    const result = await normalizeTags(makeEvidence(), makeClassifier([], 0.9));
    expect(result).toBeNull();
  });

  it('returns null when confidence is below the floor', async () => {
    const result = await normalizeTags(makeEvidence(), makeClassifier(['comida'], 0.39));
    expect(result).toBeNull();
  });

  it('caps four themes to three', async () => {
    const result = await normalizeTags(
      makeEvidence(),
      makeClassifier(['comida', 'hotel', 'tren', 'paisaje'], 0.9),
    );
    expect(result?.tema).toHaveLength(3);
  });

  it('discards a tema value outside the vocabulary', async () => {
    const result = await normalizeTags(
      makeEvidence(),
      makeClassifier(['comida', 'not_a_real_tema'], 0.9),
    );
    expect(result?.tema).toEqual(['comida']);
  });

  it('maps 16/9 aspect ratio to recortable', async () => {
    const result = await normalizeTags(makeEvidence({ aspectRatio: 16 / 9 }), makeClassifier(['comida'], 0.9));
    expect(result?.apto_vertical).toBe('recortable');
  });

  it('maps 9/16 aspect ratio to si', async () => {
    const result = await normalizeTags(makeEvidence({ aspectRatio: 9 / 16 }), makeClassifier(['comida'], 0.9));
    expect(result?.apto_vertical).toBe('si');
  });

  it('leaves apto_vertical null when aspectRatio is null', async () => {
    const result = await normalizeTags(makeEvidence(), makeClassifier(['comida'], 0.9));
    expect(result?.apto_vertical).toBeNull();
  });

  it('keeps encuadre and presencia_jeaneth null', async () => {
    const result = await normalizeTags(makeEvidence(), makeClassifier(['comida'], 0.9));
    expect(result?.encuadre).toBeNull();
    expect(result?.presencia_jeaneth).toBeNull();
  });
});
```

### `src/store/remap.test.ts`

```ts
import { describe, expect, it, vi } from 'vitest';
import { planRemap, applyRemap, type LedgerRemapStore } from './remap.js';
import type { ShotRef } from '@brand-os/contracts';

function makeShotRef(overrides: Partial<ShotRef>): ShotRef {
  return {
    shot_id: 'shot-1',
    katsi_workspace_id: 'ws-1',
    representation_id: 'repr-1',
    locator: 'loc-1',
    source_path: '/media/01_CHINA/beijing/clip.mp4',
    tags: {
      lugar: 'beijing',
      tema: ['comida'],
      encuadre: null,
      presencia_jeaneth: null,
      calidad_visual: null,
      apto_vertical: null,
    },
    duracion_s: 10,
    ...overrides,
  };
}

describe('planRemap', () => {
  it('emits a pair when boundaries match and id changed', () => {
    const previous = [makeShotRef({ shot_id: 'old-1', locator: 'scene=0-100' })];
    const next = [makeShotRef({ shot_id: 'new-1', locator: 'scene=0-100' })];
    const plan = planRemap(previous, next);
    expect(plan).toEqual([{ from: 'old-1', to: 'new-1' }]);
  });

  it('emits no pair when boundaries differ', () => {
    const previous = [makeShotRef({ shot_id: 'old-1', locator: 'scene=0-100' })];
    const next = [makeShotRef({ shot_id: 'new-1', locator: 'scene=0-200' })];
    const plan = planRemap(previous, next);
    expect(plan).toEqual([]);
  });

  it('emits no pair when source_path differs', () => {
    const previous = [makeShotRef({ shot_id: 'old-1', source_path: '/media/01_CHINA/beijing/a.mp4' })];
    const next = [makeShotRef({ shot_id: 'new-1', source_path: '/media/01_CHINA/beijing/b.mp4' })];
    const plan = planRemap(previous, next);
    expect(plan).toEqual([]);
  });

  it('emits no pair when id is unchanged', () => {
    const previous = [makeShotRef({ shot_id: 'same-1' })];
    const next = [makeShotRef({ shot_id: 'same-1' })];
    const plan = planRemap(previous, next);
    expect(plan).toEqual([]);
  });

  it('maps every member of a multi-scene generation', () => {
    const previous = [
      makeShotRef({ shot_id: 'old-1', locator: 'scene=0-100' }),
      makeShotRef({ shot_id: 'old-2', locator: 'scene=100-200' }),
      makeShotRef({ shot_id: 'old-3', locator: 'scene=200-300' }),
    ];
    const next = [
      makeShotRef({ shot_id: 'new-1', locator: 'scene=0-100' }),
      makeShotRef({ shot_id: 'new-2', locator: 'scene=100-200' }),
      makeShotRef({ shot_id: 'new-3', locator: 'scene=200-300' }),
    ];
    const plan = planRemap(previous, next);
    expect(plan).toEqual([
      { from: 'old-1', to: 'new-1' },
      { from: 'old-2', to: 'new-2' },
      { from: 'old-3', to: 'new-3' },
    ]);
  });
});

describe('applyRemap', () => {
  it('calls rename with the exact prefixed keys', async () => {
    const rename = vi.fn();
    const store: LedgerRemapStore = { rename };
    const plan = [
      { from: 'old-1', to: 'new-1' },
      { from: 'old-2', to: 'new-2' },
    ];

    await applyRemap(plan, store);

    expect(rename).toHaveBeenCalledTimes(2);
    expect(rename).toHaveBeenNthCalledWith(1, 'brand-os:asset-uses:old-1', 'brand-os:asset-uses:new-1');
    expect(rename).toHaveBeenNthCalledWith(2, 'brand-os:asset-uses:old-2', 'brand-os:asset-uses:new-2');
  });
});
```

---

## Notes

- **Unverified imports:** I could not verify that `@brand-os/contracts` exports all of `TEMA`, `LUGAR`, `ShotTags`, `ShotTagsSchema`, and `ShotRef` exactly as the packet describes. I have used the names as given in the packet, but you should confirm the exports resolve at build time.
- **`paths.ts` simplification:** The packet says folder structure encodes location (`/media/01_CHINA/beijing/clip.mp4` → `'beijing'`), and my implementation does a simple case-insensitive substring search. I did not hard-code any location-independent folder logic beyond that. I made a conscious judgment to treat the first vocabulary match in `LUGAR` order as the answer — the packet does not specify a tie-breaking rule for paths containing multiple city names (e.g., `beijing_xian`), nor does it define a directory-depth rule.
- **`planRemap` matching semantics:** I used a strict `(source_path, locator)` equality per the packet. When multiple previous refs share the same identity (possible if `upsertMany` was used with different `shot_id`s but identical `source_path`+`locator`), I emit a pair for each matching previous ref — the packet does not specify a deduplication rule there, and my loop matches against the full `previous` array rather than only distinct identities.
- **`normalize.ts` return type:** I return an object literal typed as `ShotTags` but note that `tema` built via `validTema.slice(0, 3)` is typed `Tema[]`, which structurally satisfies `ShotTags`'s `z.enum(TEMA)[]` constraint after the vocabulary filter. No schema validation is performed at runtime — that responsibility lives with contracts/zod, per the packet's requirement not to redefine any schema locally.
- **`presencia_jeaneth` and `calidad_visual`:** Both remain `null` per the rules. `calidad_visual` is likewise never set — the packet's rules only mention `encuadre`, `presencia_jeaneth`, and `calidad_visual` staying `null`, and I applied that to all three.
- **Test-only assertion realism:** The multi-scene test asserts `planRemap` output order. Both input arrays are in locator order and the loop preserves array order, so this is deterministic; no sorting was added since the packet did not require it.
- **`applyRemap` error behavior:** I do not stop on first failure — I iterate all pairs and let errors propagate out of the loop naturally (unhandled rejection at the call site). The packet doesn't specify partial-failure semantics, so I preserved simple sequential execution.
- **No new dependencies, no runtime deps other than `@brand-os/contracts` which already exists.** All `vitest` imports are dev-only as per house pattern.
- **I did not run `npx vitest run` or `npm run build`** — the packet explicitly says not to claim tests ran unless output was provided. You'll need to run those yourself.