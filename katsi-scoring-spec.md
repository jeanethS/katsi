# katsi Scoring Substrate Spec

Status: **Draft — design review, no code written yet.**
Scope: replaces the fixed two-term fusion in `retrieve/search.py` with a central weight table and additive evidence, makes ranking deterministic, and gives `MENTIONS` / `ABOUT` real edge weights so Leiden rollups can gate on them.
Prior art: GitNexus `gitnexus-shared/src/scope-resolution/evidence-weights.ts` (additive evidence table, clamp, deterministic tie-break cascade).

---

## 1. Why this exists

Today `search.py` scores a file with two numbers:

```python
# search.py:94, :111, :125  (current)
vw, gw = s.retrieve.vector_weight, s.retrieve.graph_weight   # 0.6 / 0.4
graph_score = 1.0            # binary — any neighbor of any top hit
score = vw * vec + gw * graph_score
```

Three problems fall out of those three lines:

1. **The graph term is binary.** A file sharing eleven entities with the top hit and a file sharing one entity both get `graph_score = 1.0`, so both get exactly `+0.4`. The graph knows the difference; the score throws it away. The `weight DOUBLE` column already on `MENTIONS` and `ABOUT` (`graph.py:50,:54`) is written as a constant `1.0` (`graph.py:91,:101`) and never read.
2. **`why` is one string, chosen by an if/elif chain** (`search.py:112-121`). A file can match by vector *and* share an entity *and* be referenced — the user sees one reason, picked by whichever branch fired first. There is no record of what actually produced the number.
3. **Ranking is not reproducible.** `fused.sort(key=lambda h: h.score, reverse=True)` (`search.py:130`) is stable with respect to *input order*, but the input is `candidate_files = set(top_files) | set(neighbor_files.keys())` (`search.py:95`) — a `set`, whose iteration order varies with insertion history and, across processes, with `PYTHONHASHSEED`. Equal scores therefore come back in arbitrary order. Every consumer downstream (`build_context`, prompt caching, tests) inherits that non-determinism.

The Leiden rollup work needs per-edge weights to filter garbage entities out of the File↔File projection. Retrieval needs per-edge weights to stop treating one shared entity like eleven. **Same substrate.** Build it once, here, and the rollup pass consumes it.

### Layman version

Right now, ranking a file is like grading an essay on two criteria: "does it look relevant" (a percentage) and "is it connected to anything relevant" (yes/no). The yes/no is the problem — deeply connected and barely connected score identically.

The fix is a **rubric**: a fixed table of named line items, each worth a stated number of points. Add up the points a file earns, keep the receipt of which lines fired. That receipt is the same thing as the explanation of *why* the file ranked where it did — you don't compute the score and then separately invent an explanation for it, which is what the code does today. The receipt **is** the score.

That is exactly what GitNexus does to decide "which function does this call refer to": one exported table of weights, sum them, clamp to `[0,1]`, keep each contribution in an `evidence` array for auditing. It scales to large repos and stays debuggable, because when a rank looks wrong you read the receipt instead of re-deriving the arithmetic.

---

## 2. Before / after

```
BEFORE

  query ─ embed ─ vectors.search ─ best chunk per file ─┐
                                                        ├─ score = 0.6*vec + 0.4*(1.0 or 0.0)
  top_files ─ graph.neighbors(hops=1) ─ first via wins ─┘        │
                                                                 └─ why = "shares entity with top hit"   (one string)
                                                                 └─ sort(score) over a set()             (ties arbitrary)


AFTER

  query ─ embed ─ vectors.search ─ best chunk per file ──────────┐
                                                                 │
  top_files ─ graph.neighbors(hops=1, min_weight=gate) ──────────┤
              │  edge weight read from MENTIONS/ABOUT.weight     │
              │  edges below gate never returned                 │
              ▼                                                  ▼
        [Evidence, Evidence, ...]  ────────────────────►  score_file()
         kind / weight / detail                              │
                                                             ├─ sum contributions
                                                             ├─ clamp [0.0, 1.0]
                                                             ▼
                                                          FileHit
                                                            score     = clamped sum
                                                            evidence  = [Evidence, ...]   (new, full receipt)
                                                            why       = render(evidence)  (derived, unchanged type)
                                                             │
                                                             ▼
                                                    rank_hits()  — 4-level tie-break cascade
                                                             │
                                                             ▼
                                                    deterministic list[FileHit]

  Same Evidence weights, same gate ──► graph/rollup.py  File↔File projection (Leiden)
```

The gate and the weight table sit *below* both consumers. Retrieval and rollups read the same numbers from the same place.

---

## 3. Component 1 — Central weight table + additive confidence

### 3.1 Where the table lives

Global `CLAUDE.md` is explicit: *"Never hardcode model names / paths / thresholds — read from this Settings object."* So the table is a Pydantic model in `config.py`, not a module-level `const` as in GitNexus's TypeScript. Defaults live in the class; `katsi.toml` and `KATSI_RETRIEVE__WEIGHTS__*` override.

```python
# packages/core/katsi_core/config.py  (new)

class RetrievalWeights(BaseModel):
    """Every number that can move a file's rank. Nothing outside this class
    may contribute to a score. Inline literals in scoring code are a defect.
    """

    # Base: vector similarity, already in [0,1], scaled by this.
    vector: float = 0.50

    # Graph evidence. `per_shared` is multiplied by the summed edge weight of
    # the shared connectors, then capped — so 11 shared entities beats 1, but
    # cannot alone outrank a strong vector match.
    entity_per_shared: float = 0.12
    entity_cap: float = 0.30
    topic_per_shared: float = 0.08
    topic_cap: float = 0.20

    # Structural edges. An explicit outbound reference is the strongest
    # non-vector signal: the author wrote the link by hand.
    reference_out: float = 0.25
    reference_in: float = 0.15

    # Near-duplicate of a top hit carries no new information for a context
    # bundle — it costs tokens to say the same thing twice. Slight penalty.
    duplicate_of: float = -0.05

    # Distance decay, mirroring GitNexus `scopeChainPerDepth: -0.02`.
    # Applied per hop beyond the first.
    per_extra_hop: float = -0.02

    # Clamp bounds. Scores outside are pinned, never rejected.
    score_min: float = 0.0
    score_max: float = 1.0


class RetrieveSettings(BaseModel):
    top_k_chunks: int = 16
    top_k_files: int = 8
    graph_expand_hops: int = 1
    vector_weight: float = 0.6          # DEPRECATED — see §3.5
    graph_weight: float = 0.4           # DEPRECATED — see §3.5
    default_context_max_tokens: int = 3000
    weights: RetrievalWeights = Field(default_factory=RetrievalWeights)
    min_edge_weight: float = 0.35       # the gate — see §5
```

**Defaults are calibrated, not arbitrary.** The invariants they satisfy:

| Invariant | Arithmetic | Rationale |
|---|---|---|
| Perfect vector match alone does not saturate | `0.50 * 1.0 = 0.50` | Leaves headroom for graph evidence to reorder. |
| Max graph-only evidence cannot beat a good vector match alone | `0.30 + 0.20 + 0.25 = 0.75` vs `0.50` | A pure graph neighbor *can* outrank a mediocre vector hit — intended, that is the point of fusion — but it takes several independent signals to do it. |
| Vector + one shared entity beats vector alone | `0.50*v + 0.12` | Monotonic: adding evidence never lowers the rank. |
| Everything at once clamps rather than runs away | `0.50 + 0.30 + 0.20 + 0.25 + 0.15 = 1.40 → 1.0` | Clamp is expected, not an error. |

These are **starting values**, to be tuned against the golden-set test in §7. Their job right now is to be visible in one place, so tuning is a config edit rather than an archaeology expedition.

### 3.2 The Evidence model

`models.py` opens with: *"Strictly follows §5.1 of the architecture spec. Do not rename fields, do not add defaults beyond what is specified."* This spec **adds** to `FileHit`; it renames nothing and changes no existing field's type or default. `why: str` stays exactly as it is, and is now *derived* from the evidence rather than picked by an if/elif chain — so every existing consumer (MCP tools, `build_context`, the frontend `Source.why`) keeps working untouched.

```python
# packages/core/katsi_core/models.py  (additions)

class EvidenceKind(StrEnum):
    VECTOR = "vector"
    ENTITY = "entity"
    TOPIC = "topic"
    REFERENCE_OUT = "reference_out"
    REFERENCE_IN = "reference_in"
    DUPLICATE = "duplicate"
    HOP_DECAY = "hop_decay"


class Evidence(BaseModel):
    """One line item on a FileHit's score. Sums to FileHit.score (pre-clamp)."""

    kind: EvidenceKind
    contribution: float          # signed points added to the score
    detail: str                  # human-readable: "shares 3 entities: Kùzu, Leiden, bge-m3"


class FileHit(BaseModel):
    file_id: str
    path: str
    summary: str
    score: float
    why: str                            # unchanged type; now rendered from evidence
    evidence: list[Evidence] = []       # NEW — the receipt
```

`evidence` defaults to `[]`, so any `FileHit` constructed elsewhere (tests, fixtures) still validates.

### 3.3 The scorer

New file, `packages/core/katsi_core/retrieve/scoring.py`. Pure functions, no I/O, no store handles — trivially unit-testable, which is the whole reason it is its own module.

```python
def score_file(evidence: list[Evidence], weights: RetrievalWeights) -> float:
    """Sum contributions, clamp. The ONLY place a score is produced."""
    raw = sum(e.contribution for e in evidence)
    return max(weights.score_min, min(weights.score_max, raw))


def render_why(evidence: list[Evidence]) -> str:
    """Derive the legacy one-line `why` from the receipt.
    Highest-contribution kinds first; join with ' + '.
    """
```

Evidence builders, one per signal, each returning `Evidence | None`:

```python
def vector_evidence(similarity: float, w: RetrievalWeights) -> Evidence | None
def entity_evidence(shared: list[tuple[str, float]], w: RetrievalWeights) -> Evidence | None
def topic_evidence(shared: list[tuple[str, float]], w: RetrievalWeights) -> Evidence | None
def reference_evidence(direction: str, w: RetrievalWeights) -> Evidence | None
def duplicate_evidence(similarity: float, w: RetrievalWeights) -> Evidence | None
def hop_evidence(hops: int, w: RetrievalWeights) -> Evidence | None
```

`shared` is `list[(connector_name, edge_weight)]`. Entity contribution:

```
contribution = min(entity_cap, entity_per_shared * sum(edge_weights))
detail       = f"shares {len(shared)} entities: {', '.join(top 3 names)}"
```

Summing edge weights (not counting edges) is what makes §5's gate matter: a weak entity edge contributes proportionally less even when it survives the gate.

### 3.4 `search.py` after

`search()` keeps its signature and return type. Steps 1–3 (embed, ANN, aggregate per file) are unchanged. Step 4 becomes evidence collection; the fusion arithmetic moves out to `scoring.py`.

```python
for fid in candidate_files:
    ev: list[Evidence] = []
    if (e := vector_evidence(vec_best.get(fid, 0.0), w)) is not None:
        ev.append(e)
    for e in graph_evidence_for(fid, neighbor_index, w):   # entity/topic/ref/dup/hop
        ev.append(e)
    if not ev:
        continue
    fused.append(FileHit(
        file_id=fid, path=path, summary=summary,
        score=score_file(ev, w), why=render_why(ev), evidence=ev,
    ))
return rank_hits(fused)[:k]
```

**This requires a change to `graph.neighbors()`.** Today it returns `{"file_id", "via", "name"}` and dedups peers (`search.py:90`: `if peer and peer not in neighbor_files`) — so the *second* shared entity between the same pair of files is discarded. Counting shared entities is impossible on that shape. `neighbors()` must return one row per connector, carrying `weight`:

```python
def neighbors(
    self, file_id: str, hops: int = 1, *, min_weight: float | None = None
) -> list[dict]:
    """Returns one row per connector (NOT deduped by peer):
        {"file_id", "via", "name", "weight", "hops"}
    `via` values unchanged: references | mentioned-entity | shared-topic | duplicate.
    `min_weight` filters MENTIONS/ABOUT edges below the gate (§5).
    """
```

De-duplication moves to the caller, which groups connectors by peer — which is exactly the grouping the evidence builders need.

### 3.5 Deprecating `vector_weight` / `graph_weight`

`retrieve.vector_weight` and `retrieve.graph_weight` stay in `RetrieveSettings` for one release, unread, marked deprecated. Removing them immediately would silently ignore a user's existing `katsi.toml` — their tuning would stop working with no signal. Instead: on `Settings.load()`, if either differs from its default, log a warning naming `retrieve.weights.vector` as the replacement. Delete both in the release after.

---

## 4. Component 2 — Deterministic tie-break cascade

### 4.1 The defect

`fused.sort(key=lambda h: h.score, reverse=True)` (`search.py:130`) sorts a list built by iterating `candidate_files`, a `set` (`search.py:95`). Python's sort is stable, so equal scores preserve *input* order — and input order here is set-iteration order, which depends on insertion history and hash seed. Two runs over identical data can return differently ordered bundles.

That breaks three things: prompt caching (a reordered bundle is a cache miss, so we pay cloud tokens for identical work), tests (any assertion on ordering is flaky), and user trust (the same question returns sources in a different order).

Float equality makes it worse: `0.1 + 0.2 != 0.3`. Two files that *should* tie may differ by `1e-17` and order by numerical noise. So the cascade compares on a **quantized** score.

### 4.2 The cascade

```python
# retrieve/scoring.py

SCORE_QUANTUM = 1e-6   # scores within this are ties; below float noise, above meaningful

def rank_hits(hits: list[FileHit]) -> list[FileHit]:
    """Total order. Deterministic across processes and PYTHONHASHSEED."""
    return sorted(hits, key=_rank_key)

def _rank_key(h: FileHit) -> tuple:
    return (
        -round(h.score / SCORE_QUANTUM),        # 1. score, quantized, desc
        -len(h.evidence),                       # 2. more independent signals, desc
        -_vector_contribution(h),               # 3. vector similarity, desc
        h.path,                                 # 4. path lexicographic, asc — total
    )
```

Level 4 is the counterpart of GitNexus's `nodeId.localeCompare` final tie-break. `path` is the absolute realpath and `file_id = blake3(realpath)` is unique, so path is unique too — **the order is total, and no tie can reach the sort's stability fallback.** Set-iteration order becomes unobservable.

Levels 2 and 3 are ordered by defensibility: a file corroborated by three independent signals is a better bet than one riding a single signal to the same number; failing that, the vector term is the one we trust most on its own.

---

## 5. Component 3 — Confidence-gated edges

### 5.1 What is wrong now

```python
# graph.py:91
def add_mentions(self, file_id: str, entities: list[dict], weight: float = 1.0) -> None:
```

Every entity the LLM emits gets `weight = 1.0`. A hallucinated entity from one throwaway sentence weighs the same as the project name appearing in every chunk. In the Leiden File↔File projection, those junk entities are exactly what fabricate false communities — two unrelated files "sharing" a garbage entity become an edge, and Leiden has no way to know it is noise.

### 5.2 Where confidence comes from

Two sources, in order:

1. **LLM-declared confidence.** `Extraction.entities` is `list[dict]` typed as `{"name": str, "kind": "person|org|project"}`. Extend the *prompt contract* to request an optional `confidence: float` in `[0,1]` per entity. Because `entities` is already `list[dict]`, **this needs no change to `models.py`** — the §5.1 field contract holds. When present, use it.
2. **Deterministic frequency fallback.** Local models are unreliable at self-reporting confidence and may omit the field entirely. When absent, derive:

```
support   = number of distinct chunks in this file whose text contains the entity name
coverage  = support / max(1, total chunks in file)
weight    = clamp(0.30 + 0.70 * coverage, 0.0, 1.0)
```

An entity grounded in one chunk of a twelve-chunk file lands near `0.36`; one appearing throughout lands near `1.0`. When both sources exist, `weight = 0.5 * declared + 0.5 * frequency` — the frequency term is evidence the model cannot fake, so it always gets a vote.

Same scheme for `ABOUT` / topics, matching on topic string.

This is the one place this spec deviates from GitNexus. GitNexus reads a **syntax tree** — its evidence is structural and exact (this import statement resolves to that file). katsi reads **prose through an LLM** — the extraction itself is probabilistic, so the confidence must come partly from something deterministic. Frequency is that anchor.

### 5.3 The gate

`retrieve.min_edge_weight = 0.35` (default). Applied in two places, both reading the same setting:

- `graph.neighbors(..., min_weight=...)` — weak edges never enter retrieval evidence.
- `graph.file_file_projection(min_weight=...)` — weak edges never enter the Leiden projection (this is the hook the rollup sketch needs).

`0.35` is deliberately just above the `0.30` floor of the frequency formula: it excludes entities with essentially no textual support, while keeping anything that recurs at all. Edges below the gate are **kept in the graph, not deleted** — they are filtered at read time. Deleting would make the gate unturnable without a full reindex, and a threshold you cannot lower to inspect what it removed is a threshold you cannot debug.

### 5.4 Migration

`weight DOUBLE` already exists on both rel tables — **no Kùzu schema migration is needed for this component.** But every edge written so far holds the literal `1.0`, so on an existing DB the gate passes everything and the feature is inert. It is not wrong, just not yet doing anything.

Rather than a backfill (recomputing frequency needs chunk text re-read for every file — that is a reindex wearing a hat), lean on the machinery that already exists: `IngestPipeline.index_file` skips when `content_hash` is unchanged **and** `status == INDEXED` (`pipeline.py:~138`). Add a schema version marker to the record store; when it lags, treat records as `STALE` so the next index pass rewrites their edges with real weights. Users who never reindex keep today's behavior exactly — a strictly safe default.

---

## 6. Files touched

| File | Change | Risk |
|---|---|---|
| `core/katsi_core/retrieve/scoring.py` | **New.** Weight application, evidence builders, `score_file`, `render_why`, `rank_hits`. Pure. | Low — new, isolated. |
| `core/katsi_core/config.py` | Add `RetrievalWeights`; add `weights` + `min_edge_weight` to `RetrieveSettings`; deprecate `vector_weight`/`graph_weight`. | Low — additive, defaults preserved. |
| `core/katsi_core/models.py` | Add `EvidenceKind`, `Evidence`; add `FileHit.evidence: list[Evidence] = []`. | Low — additive; `why` unchanged. |
| `core/katsi_core/retrieve/search.py` | Fusion arithmetic out to `scoring.py`; collect evidence; group connectors per peer; `rank_hits`. | **Medium — the real work.** Signature and return type unchanged. |
| `core/katsi_core/store/graph.py` | `neighbors()` returns per-connector rows with `weight` + `hops`, accepts `min_weight`; `add_mentions`/`add_about` take per-entity weights. | **Medium — `neighbors()` shape change.** Callers: `search.py`, `related` MCP tool. |
| `core/katsi_core/ingest/pipeline.py` | Compute per-entity/topic weights; pass to graph writes. | Low-medium. |
| `core/katsi_core/ingest/extract.py` | Prompt asks for optional per-entity `confidence`. | Low — optional field, fallback exists. |

Not touched: `synth.py`, `retrieve/context.py`, MCP tool signatures, the frontend. `Source.why` in `frontend/src/api/types.ts` still receives a string.

Sequencing (each step green before the next):

1. `models.py` + `config.py` — additive, nothing reads them yet.
2. `scoring.py` + its unit tests — pure, no stores needed.
3. `graph.py` `neighbors()` reshape + `related` tool caller.
4. `search.py` rewire onto `scoring.py`.
5. `pipeline.py` + `extract.py` weight population.
6. Golden-set calibration (§7).

Steps 1–4 ship value with `weight = 1.0` everywhere: evidence receipts and deterministic ranking work immediately. Step 5 turns the gate live.

---

## 7. Tests

Per global testing rules: tests first (RED), 80% minimum coverage, AAA structure.

**`scoring.py` (unit, no I/O):**
- `test_score_is_sum_of_evidence_contributions`
- `test_score_clamps_at_one_when_all_evidence_present`
- `test_score_clamps_at_zero_when_only_penalties_apply`
- `test_entity_evidence_caps_at_entity_cap` — 20 shared entities must not exceed `entity_cap`
- `test_entity_evidence_scales_with_edge_weight` — same count, lower weights, lower contribution
- `test_adding_evidence_never_lowers_score` — monotonicity (property test over generated evidence lists)
- `test_render_why_orders_by_contribution_descending`
- `test_duplicate_evidence_penalizes`

**`rank_hits` (unit):**
- `test_rank_hits_orders_by_score_descending`
- `test_ties_break_by_evidence_count_then_vector_then_path`
- `test_identical_hits_in_shuffled_input_produce_identical_order` — shuffle input N times, assert one output
- `test_scores_within_quantum_are_treated_as_tied` — `0.1+0.2` vs `0.3` must tie, then break on path
- **Determinism across processes:** run the ranking in a subprocess under two different `PYTHONHASHSEED` values, assert identical output. This is the test that actually pins the §4 defect; an in-process test cannot see hash-seed variance.

**`search.py` (integration, fake stores):**
- `test_search_returns_evidence_for_every_hit`
- `test_file_sharing_more_entities_outranks_file_sharing_one` — **the regression test for the binary `graph_score`**; fails on today's code
- `test_search_respects_min_edge_weight_gate`
- `test_why_still_populated_for_legacy_consumers`

**Calibration (golden set):** ~20 hand-ranked (query, expected top-3) pairs over a fixture corpus, asserting nDCG@3 does not regress below the current implementation's baseline. This is what makes §3.1's numbers tunable rather than superstitious — measure the baseline *before* touching `search.py`, or there is nothing to compare against.

---

## 8. Open decisions

1. **`duplicate_of: -0.05`.** Argument for: a near-duplicate of a top hit spends context tokens restating it. Against: sometimes the duplicate is the copy the user actually wants (the one in the right folder). A small penalty rather than exclusion hedges this, but the sign is a judgement call — flag for review.
2. **`reference_in = 0.15` requires reverse traversal.** `REFERENCES` is directed `FROM File TO File`; today `neighbors()` only walks it outbound. Inbound needs a second Cypher match. Worth it — "what points at this?" is a strong signal — but it is extra query cost per candidate.
3. **Hop decay is currently dead weight.** `retrieve.graph_expand_hops` defaults to `1` and `search.py:88` hardcodes `hops=1`, so `per_extra_hop` never fires. Spec'd now so multi-hop expansion does not need a scoring change later. Alternative: drop it until hops > 1 ships (YAGNI).
4. **`0.5/0.5` blend of declared vs frequency confidence** (§5.2) is unmeasured. Should be a config knob if the golden set shows sensitivity.

---

## 9. Takeaway

The whole spec is one idea applied three times: **make the number and the reason for the number the same object.** The weight table says what evidence is worth; the evidence list records what fired; the score is their sum; `why` is their rendering; the tie-break reads the same fields. Nothing computes a rank the receipt cannot explain.

The reason to do this before the Leiden rollups: rollups need per-edge weights to keep garbage entities from fabricating communities, and retrieval needs the same weights to stop treating one shared entity like eleven. One substrate, two consumers. Building rollups first would mean either inventing a second weighting scheme or hardcoding the gate — and then reconciling them later, on a graph that is by then full of edges written under the old rules.
