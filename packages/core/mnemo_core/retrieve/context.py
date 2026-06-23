from __future__ import annotations

import os

from mnemo_core.clients.embed import EmbedClient
from mnemo_core.config import Settings
from mnemo_core.ingest.records import FileRecordStore
from mnemo_core.models import Chunk, ContextBundle, FileHit
from mnemo_core.retrieve.search import search
from mnemo_core.store.graph import GraphStore
from mnemo_core.store.vectors import VectorStore


def _name(path: str) -> str:
    return os.path.basename(path) or path


def _find_path(file_id: str, hits: list[FileHit]) -> str:
    for h in hits:
        if h.file_id == file_id:
            return h.path
    return file_id


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
            out.append((
                fid,
                row.get("id", ""),
                row.get("text", ""),
                1.0 / (1.0 + float(row.get("_distance", 0.0))),
            ))
            seen.add(fid)
        if len(seen) == len(wanted):
            break
    return out


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
       filtered to file_id == hit.file_id -- pick the BEST chunk per file.
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
           "{file_name} --MENTIONS--> {entity_name}"
           "{file_name} --REFERENCES--> {neighbor_name}"
       Each line contributes ~10 tokens -- include them in token_estimate too.
    6. token_estimate is the sum of (file summaries ~= estimated) + chunks + lines.
    """
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
        existing_nbs: list[str] = []
        for nb in graph.neighbors(hit.file_id, hops=1):
            peer_id = nb.get("file_id")
            peer_name = nb.get("name") or peer_id
            if peer_id in in_bundle_files:
                via = nb.get("via", "related")
                if via == "mentioned-entity":
                    existing_nbs.append(f"MENTIONS-> {peer_name}")
                elif via == "shared-topic":
                    existing_nbs.append(f"TOPIC-> {peer_name}")
                elif via == "references":
                    existing_nbs.append(f"REFERENCES-> {_name(_find_path(peer_id, hits))}")
                else:
                    existing_nbs.append(f"DUPLICATE_OF-> {_name(_find_path(peer_id, hits))}")
        if existing_nbs:
            line = f"{_name(hit.path)} -- " + "; ".join(existing_nbs)
            rels.append(line)
            tokens_used += 10  # rough per-line estimate

    # Truncate relationships if they would push us WAY over budget (cap at
    # 20 lines, ~200 tokens).
    if tokens_used > max_tokens and rels:
        rels = []

    return ContextBundle(
        query=query,
        files=hits,
        chunks=included_chunks,
        relationships=rels,
        token_estimate=tokens_used,
    )
