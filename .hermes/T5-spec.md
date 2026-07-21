# T5 — Retrieval + context assembly

Extends the existing katsi workspace. T0–T4 already done — add only the new files.

## TOOL RULES (read first)

Do NOT explore any codebase. Do NOT search for anything.
Use ONLY your file-write/edit tool and `bash` (for `uv run pytest`, `uv run ruff check .`).

When done run `uv run pytest -q 2>&1 | tail -20` and `uv run ruff check . 2>&1 | tail -10`
from the project root, report exit codes + tail outputs.

## 0. What you wire together

From `katsi_core.models`: `FileHit`, `ContextBundle`, `Chunk`, `FileRecord`.
From `katsi_core.config`: `Settings`.
From `katsi_core.store.graph`: `GraphStore`.
From `katsi_core.store.vectors`: `VectorStore`.
From `katsi_core.clients.embed`: `EmbedClient`.
From `katsi_core.ingest.records`: `FileRecordStore`.

The architecture spec §7.2 retrieval flow:
1. Embed query → LanceDB ANN, top-N chunks.
2. Graph-expand: resolve those chunks' files, pull 1-hop neighbors.
3. Score fusion: combine vector score + graph proximity.
4. Assemble `ContextBundle` under `max_tokens`.

## 1. Files to create (5 new files)

```
packages/core/katsi_core/retrieve/__init__.py
packages/core/katsi_core/retrieve/search.py
packages/core/katsi_core/retrieve/context.py
tests/test_search.py
tests/test_context.py
```

## 2. Contract: `packages/core/katsi_core/retrieve/__init__.py`

```python
"""katsi retrieval: vector+graph fusion + budget-capped context bundle."""
```

## 3. Contract: `packages/core/katsi_core/retrieve/search.py`

Score fusion of vector ANN + graph 1-hop expansion. Returns ranked `FileHit`s with
a short `why` line.

```python
from __future__ import annotations

from katsi_core.clients.embed import EmbedClient
from katsi_core.config import Settings
from katsi_core.ingest.records import FileRecordStore
from katsi_core.models import FileHit
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore

# Why strings:
WHY_VECTOR = "vector match"
WHY_ENTITY = "shares entity with top hit"
WHY_TOPIC = "shares topic with top hit"
WHY_REF_OUT = "referenced by top hit"
WHY_DUPLICATE = "duplicate of top hit"


def search(
    query: str,
    k: int = 8,
    *,
    settings: Settings | None = None,
    vectors: VectorStore | None = None,
    graph: GraphStore | None = None,
    embed: EmbedClient | None = None,
    records: FileRecordStore | None = None,
) -> list[FileHit]:
    """Fused vector+graph search. Returns up to k FileHits ranked by combined
    score (descending).

    Pipeline:
    1. Optionally resolve settings/stores lazily (deferred like IngestPipeline).
    2. embed .embed([query]) → 1 vector.
    3. vectors.search(vec, top_n_chunks) → list[(chunk_id, file_id, vector_score)]
       where top_n_chunks = settings.retrieve.top_k_chunks.
    4. Aggregate chunk scores per file: best chunk score per file_id wins.
    5. For each ranked-by-vector file_id (top candidates), call graph.neighbors(file_id)
       to get peer file_ids + their via + name fields.
    6. Score fusion:
        vector_score_norm = vector_score  (already similarity, in [0, 1])
        graph_score = 1.0 if this file appears as a neighbor of ANY top-ranked hit,
                      else 0.0
        fused_score = settings.retrieve.vector_weight * vector_score
                    + settings.retrieve.graph_weight * graph_score
    7. Sort files descending by fused_score; slice top k.
    8. Build FileHit for each: file_id, path, summary, score=fused_score, why=one of:
        WHY_VECTOR if file was in the top vector hits.
        WHY_ENTITY / WHY_TOPIC / WHY_REF_OUT / WHY_DUPLICATE if surfaced via graph.
        If both, choose graph reason ("graph-extended: " + the why).
    """
```

Implementation reference:

```python
def _resolve(settings, store_obj, factory):
    return store_obj if store_obj is not None else factory()


def search(
    query, k=8, *, settings=None, vectors=None, graph=None,
    embed=None, records=None,
):
    s = settings or Settings()
    vectors = _resolve(s, vectors, lambda: VectorStore(s.store.data_dir / "vectors", s.store.lancedb_table))
    graph = _resolve(s, graph, lambda: GraphStore(s.store.data_dir / s.store.kuzu_db))
    embed = _resolve(s, embed, lambda: EmbedClient(s))
    records = _resolve(s, records, lambda: FileRecordStore(s.store.data_dir / "records"))

    if not query.strip():
        return []

    # 1. embed + ANN
    qv = embed.embed([query])[0]
    top_n = s.retrieve.top_k_chunks
    raw_hits = vectors.search(qv, k=top_n)       # list[(chunk_id, file_id, score)]
    if not raw_hits:
        return []

    # 2. aggregate per-file: keep best vector score per file_id
    file_vec_score: dict[str, float] = {}
    for _chunk_id, file_id, score in raw_hits:
        prev = file_vec_score.get(file_id, -1.0)
        if score > prev:
            file_vec_score[file_id] = score

    top_files = list(file_vec_score.keys())
    vec_best = {fid: file_vec_score[fid] for fid in top_files}

    # 3. graph expand: for each top file, get its 1-hop neighbors
    neighbor_files: dict[str, tuple[str, str | None]] = {}  # peer_fid -> (via, name)
    for src_fid in top_files:
        for nb in graph.neighbors(src_fid, hops=1):
            peer = nb.get("file_id")
            if peer and peer not in neighbor_files and peer != src_fid:
                neighbor_files[peer] = (nb.get("via", "graph"), nb.get("name"))

    # 4. score fusion
    vw, gw = s.retrieve.vector_weight, s.retrieve.graph_weight
    candidate_files = set(top_files) | set(neighbor_files.keys())
    fused: list[FileHit] = []
    for fid in candidate_files:
        vnode = graph.get_file(fid)
        if vnode is None:
            records_rec = records.get(fid)
            if records_rec is None:
                continue
            path = records_rec.path
            summary = records_rec.summary or ""
        else:
            path = vnode.path
            summary = vnode.summary or ""
        vec = vec_best.get(fid, 0.0)
        if fid in neighbor_files:
            via, name = neighbor_files[fid]
            graph_score = 1.0
            if via == "mentioned-entity":
                why = WHY_ENTITY
            elif via == "shared-topic":
                why = WHY_TOPIC
            elif via == "references":
                why = WHY_REF_OUT
            else:
                why = WHY_DUPLICATE
            if fid in vec_best:
                why = f"vector + graph-extended ({why})"
        else:
            graph_score = 0.0
            why = WHY_VECTOR
        score = vw * vec + gw * graph_score
        fused.append(FileHit(file_id=fid, path=path, summary=summary,
                                 score=score, why=why))
    fused.sort(key=lambda h: h.score, reverse=True)
    return fused[:k]
```

The implementation reference above is the source of truth — use it verbatim,
adjust imports as needed.

## 4. Contract: `packages/core/katsi_core/retrieve/context.py`

```python
from __future__ import annotations

from katsi_core.clients.embed import EmbedClient
from katsi_core.config import Settings
from katsi_core.ingest.records import FileRecordStore
from katsi_core.models import Chunk, ContextBundle, FileHit
from katsi_core.retrieve.search import search
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore


def build_context(
    query: str,
    max_tokens: int = 3000,
    *,
    settings: Settings | None = None,
    vectors: VectorStore | None = None,
    graph: GraphStore | None = None,
    embed: EmbedClient | None = None,
    records: FileRecordStore | None = None,
) -> ContextBundle:
    """Assemble a budget-capped ContextBundle for the client's model.

    1. Call search(query, k=settings.retrieve.top_k_files) for fused FileHits.
    2. For each hit, grab its top chunks via vectors.search(qv, k=top_k_chunks)
       filtered to file_id == hit.file_id — pick the BEST chunk per file.
       Don't re-embed; reuse the vector search results from a vector.fetch.
       (Alternative for v0.1 simplicity: do an extra vectors.search(qv, k=...)
       call and bucket by file_id, take 1-2 chunks per present file.)
    3. Compute initial token budget: number of files = len(hits). Reserve
       ~200 tokens per file for the summary; the rest for the top raw chunks.
    4. Iterate ranks: include the top chunk per file from step 2 in order of
       fused_score descending, until adding the next chunk would cross
       max_tokens. Stop. Each included chunk contributes chunk.token_count
       to the running token_estimate.
    5. Build relationship lines: for each file in the bundle that has graph
       neighbors in the bundle, append a human-readable line like:
           "{file_name} —MENTIONS→ {entity_name}"
           "{file_name} —REFERENCES→ {neighbor_name}"
       Each line contributes ~10 tokens — include them in token_estimate too.
    6. token_estimate is the sum of (file summaries ~= estimated) + chunks + lines.
    """
```

Implementation reference:

```python
def _name(path: str) -> str:
    import os
    return os.path.basename(path) or path


def build_context(
    query, max_tokens=3000, *, settings=None, vectors=None, graph=None,
    embed=None, records=None,
):
    s = settings or Settings()
    vectors = vectors or VectorStore(s.store.data_dir / "vectors", s.store.lancedb_table)
    graph = graph or GraphStore(s.store.data_dir / s.store.kuzu_db)
    embed = embed or EmbedClient(s)
    records = records or FileRecordStore(s.store.data_dir / "records")

    hits = search(query, k=s.retrieve.top_k_files,
                  settings=s, vectors=vectors, graph=graph,
                  embed=embed, records=records)
    if not hits:
        return ContextBundle(
            query=query, files=[], chunks=[],
            relationships=[], token_estimate=0,
        )

    # 1 vector call covers the whole bundle.
    qv = embed.embed([query])[0]
    raw = vectors.search(qv, k=s.retrieve.top_k_chunks)
    # group chunks per file, keep best-scoring per file
    file_best_chunks: dict[str, Chunk] = {}
    file_best_score: dict[str, float] = {}
    for chunk_id, file_id, score in raw:
        prev_score = file_best_score.get(file_id, -1.0)
        if score > prev_score:
            file_best_score[file_id] = score
            file_best_chunks[file_id] = Chunk(
                id=chunk_id, file_id=file_id, ordinal=-1,
                text="<raw chunk>", token_count=-1,
            )
            # NOTE: we don't have the raw chunk text from VectorStore.search output.
            # The T1 spec returns (chunk_id, file_id, score) ONLY. So we have to
            # fetch the chunk text separately. Add a helper below to fetch from
            # the vector store.
    # Resolve chunk text via vectors.fetch_chunks(file_id, limit=1) below.
    # NOTE: the T1 VectorStore contract does NOT include a fetch_chunks method.
    # For v0.1, we ride on the EXISTING search output and accept that we have
    # chunk_id + score but NO raw chunk text. Workaround in §5/§6 below.
    ...
```

The VectorStore from T1 only returns (chunk_id, file_id, score) tuples from
.search() — not the chunk text. That's an intentional simplification in T1's
spec. To get the actual `Chunk` text for the bundle, build_context needs the
text. Two clean options:

(a) Add a new method to VectorStore: `get_chunks_by_id(ids: list[str]) -> list[Chunk]`.
    This MODIFIES vectors.py from T1. The T5 spec forbids modifying T1 code, so this
    is off-limits.

(b) Add a new method to VectorStore: `get_chunks_by_file(file_id, limit)`.
    Same restriction.

(c) Re-do a search and read the text from the LanceDB table directly inside
    context.py. This is ugly but does NOT modify T1.

For v0.1 pragmatism, option (c) is what you must do: implement
`_fetch_files_top_chunks(vectors, file_ids, qv, per_file=1)` inside context.py
that:
- Calls `vectors.search(qv, k=top_k_chunks)` once.
- Walks the LanceDB table directly via `vectors._tbl.search(qv).limit(top_k_chunks).to_list()`
  to also get the `text` field. (You can read `vectors._tbl` — it's a private
  but reasonable internal access from the same package.)

Cleaner approach you should adopt (this is THE pattern for v0.1 — use this):

Add a small helper inside context.py:

```python
def _fetch_top_chunk_per_file(
    vectors: VectorStore, qv: list[float], file_ids: list[str], top_k: int
) -> list[tuple[str, str, str, float]]:
    """Return list of (file_id, chunk_id, chunk_text, score) tuples, one per file_id
    that matched. Uses the LanceDB table directly to also fetch the text column.
    """
    if not file_ids:
        return []
    tbl = vectors._tbl  # set by init_table
    if tbl is None:
        return []
    rows = tbl.search(qv).limit(top_k).to_list()
    wanted = set(file_ids)
    seen: set[str] = set()
    out: list[tuple[str, str, str, float]] = []
    for row in rows:
        fid = row.get("file_id")
        if fid in wanted and fid not in seen:
            out.append((fid, row.get("id", ""), row.get("text", ""),
                       1.0 / (1.0 + float(row.get("_distance", 0.0)))))
            seen.add(fid)
        if len(seen) == len(wanted):
            break
    return out
```

And estimate_tokens for chunk = len(text) // 4, max(1, ...).

Then the budget logic:

```python
import os
from katsi_core.ingest.chunk import estimate_tokens

def _name(path: str) -> str:
    return os.path.basename(path) or path

def build_context(query, max_tokens=3000, *, settings=None, vectors=None,
                  graph=None, embed=None, records=None):
    s = settings or Settings()
    vectors = vectors or VectorStore(s.store.data_dir / "vectors", s.store.lancedb_table)
    graph = graph or GraphStore(s.store.data_dir / s.store.kuzu_db)
    embed = embed or EmbedClient(s)
    records = records or FileRecordStore(s.store.data_dir / "records")

    hits = search(query, k=s.retrieve.top_k_files, settings=s, vectors=vectors,
                  graph=graph, embed=embed, records=records)
    if not hits:
        return ContextBundle(query=query, files=[], chunks=[],
                             relationships=[], token_estimate=0)

    qv = embed.embed([query])[0]
    file_ids = [h.file_id for h in hits]
    top_chunks = _fetch_top_chunk_per_file(vectors, qv, file_ids,
                                            s.retrieve.top_k_chunks)
    chunk_by_file: dict[str, tuple[str, str, float]] = {
        fid: (chunk_id, text, score) for fid, chunk_id, text, score in top_chunks
    }

    # Budget: ~200 tokens per file summary, rest for chunks.
    summary_budget = 200 * len(hits)
    tokens_used = 0
    tokens_used += summary_budget
    included_chunks: list[Chunk] = []
    # Iterate hits in score order; add the matching chunk if it fits.
    for hit in hits:
        tup = chunk_by_file.get(hit.file_id)
        if tup is None:
            continue
        chunk_id, text, _score = tup
        tc = max(1, len(text) // 4)
        if tokens_used + tc > max_tokens:
            break
        included_chunks.append(Chunk(
            id=chunk_id, file_id=hit.file_id, ordinal=-1,
            text=text, token_count=tc,
        ))
        tokens_used += tc

    # Relationship sketch lines
    rels: list[str] = []
    in_bundle_files = {h.file_id for h in hits}
    for hit in hits:
        existing_nbs = []
        for nb in graph.neighbors(hit.file_id, hops=1):
            peer_id = nb.get("file_id")
            peer_name = nb.get("name") or peer_id
            if peer_id in in_bundle_files:
                via = nb.get("via", "related")
                if via == "mentioned-entity":
                    existing_nbs.append(f"MENTIONS→ {peer_name}")
                elif via == "shared-topic":
                    existing_nbs.append(f"TOPIC→ {peer_name}")
                elif via == "references":
                    existing_nbs.append(f"REFERENCES→ {_name(_find_path(peer_id, hits))}")
                else:
                    existing_nbs.append(f"DUPLICATE_OF→ {_name(_find_path(peer_id, hits))}")
        if existing_nbs:
            line = f"{_name(hit.path)} — " + "; ".join(existing_nbs)
            rels.append(line)
            tokens_used += 10  # rough per-line estimate

    # Truncate relationships if they would push us WAY over budget (cap at
    # 20 lines, ~200 tokens).
    if tokens_used > max_tokens and rels:
        rels = []
        tokens_used -= 10 * 0  # already accounted above

    return ContextBundle(
        query=query,
        files=hits,
        chunks=included_chunks,
        relationships=rels,
        token_estimate=tokens_used,
    )


def _find_path(file_id: str, hits: list[FileHit]) -> str:
    for h in hits:
        if h.file_id == file_id:
            return h.path
    return file_id
```

Follow this implementation reference verbatim. The key invariant: token_estimate
must NEVER exceed max_tokens + small slack for the FINAL trailing relations.

## 5. Contract: `tests/test_search.py`

Each test uses a tmp_path-backed VectorStore + GraphStore + FileRecordStore, plus
fake embed/llm (llm not needed here, only embed). Build helper:

```python
import pytest
from katsi_core.clients.embed import EmbedClient
from katsi_core.config import Settings
from katsi_core.ingest.records import FileRecordStore
from katsi_core.models import Chunk, FileRecord, IndexStatus
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore


class _FakeEmbed:
    def __init__(self, dim=8):
        self.dim = dim
        self.embeds: list[list[str]] = []   # list of input-lists
    def embed(self, texts):
        self.embeds.append(list(texts))
        return [[0.5]*self.dim for _ in texts]

@pytest.fixture
def setup_stores(tmp_path):
    s = Settings()
    vectors = VectorStore(tmp_path / "vectors")
    vectors.init_table(8)
    graph = GraphStore(tmp_path / "graph")
    records = FileRecordStore(tmp_path / "records")
    return s, vectors, graph, records, _FakeEmbed()


def _index_file_summary(records, graph, file_id, path, summary, content_hash="h"):
    rec = FileRecord(id=file_id, path=path, name=path.split("/")[-1],
                     ext=".md", mime="", size_bytes=0, mtime=0.0,
                     content_hash=content_hash, status=IndexStatus.INDEXED,
                     summary=summary)
    records.put(rec)
    graph.upsert_file(rec)
    return rec
```

Tests:

- `test_search_empty_query_returns_empty` — search("", ...) → [].
- `test_search_returns_vector_hits_in_order` — insert 2 chunks (file f1 with vector
  close to query, file f2 far). vector hits rank f1 above f2; the FileHits are in
  the same order, each why == WHY_VECTOR.
- `test_search_surfaces_graph_neighbors` — f1 + f2 share entity Acme (f1 mentions
  entity, f2 also mentions entity). Vector search results only match f1.
  search should return BOTH f1 (via WHY_VECTOR) and f2 (via WHY_ENTITY).
  tmp_path fixture: f1 with summary "alpha"; f1 embed chunk vector close to query;
  f1 + f2 both MENTIONS Acme entity.
- `test_search_fused_score_better_than_pure_vector` — when f2 has a graph edge from f1
  but a low vector score, with vw=0.6 and gw=0.4, f1's fused_score (0.6 * 1.0 + 0.4 * 0.0)
  is 0.6 and f2's (0.6 * 0.0 + 0.4 * 1.0) is 0.4. Assert f1 > f2.

## 6. Contract: `tests/test_context.py`

Use the same setup_stores fixture pattern (defined in tests/test_context.py too —
copy or import via `from tests.test_search import setup_stores` if convenient,
otherwise redefine).

Tests:

- `test_build_context_empty_query_returns_empty_bundle` — empty query → empty
  ContextBundle, token_estimate == 0.
- `test_build_context_never_exceeds_max_tokens` — insert 1 file with a long
  chunk (~5000 chars). query returns it. max_tokens=300. The context_bundle's
  `token_estimate` MUST be <= 300 (the budget cap on chunks is enforced; summary
  reserve is included but if there's no slack the chunk stays out).
  Note: this test needs careful assertion — at most `max_tokens` chunk budget.
  Adapter behavior: if summary_reserve (200*n_files)+chunk > max_tokens, the
  chunk is skipped (no chunks in bundle) — the summary count still goes in.
  To make this test deterministic: insert a SMALL chunk (~300 chars = 75 tokens)
  with max_tokens=200 — summary reserve is 200 ≥ 200, so even a 1-token chunk
  cannot fit and chunks==[]. Assert token_estimate <= max_tokens + 0, and chunks == [].
- `test_build_context_includes_relationships_for_in_bundle_files` — f1 + f2 both
  in bundle and connected via MENTIONS → relationships list non-empty.
- `test_build_context_dedups_files` — only one FileHit per file_id appears in bundle.
- `test_build_context_returns_at_most_k_files` — k=2 with 3 indexed files.
- `test_build_context_includes_top_chunk_when_budget_allows` — insert 1 file with
  a chunk of ~50 chars (~13 tokens). max_tokens=500 → summary_reserve=200, chunk
  adds 13, total 213. chunks list has exactly 1 chunk.

## 7. Constraints

- Do NOT add new dependencies.
- Do NOT modify any T1–T4 files. If you need the chunk text, use `vectors._tbl`
  internally — that's an intra-package private access, allowed.
- Do NOT leave TODO comments.
- token_estimate must NEVER exceed max_tokens + small slack (0 slack is best).

## 8. Done when

- All 5 files exist with the contracts above.
- `uv run pytest` passes (existing 53 tests + ~10 retrieve = ~63+).
- `uv run ruff check .` is clean.
- The budget-cap test (test_build_context_never_exceeds_max_tokens) passes.
- Hand back a short report.
