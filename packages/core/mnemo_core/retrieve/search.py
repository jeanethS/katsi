from __future__ import annotations

from mnemo_core.clients.embed import EmbedClient
from mnemo_core.config import Settings
from mnemo_core.ingest.records import FileRecordStore
from mnemo_core.models import FileHit
from mnemo_core.store.graph import GraphStore
from mnemo_core.store.vectors import VectorStore

# Why strings:
WHY_VECTOR = "vector match"
WHY_ENTITY = "shares entity with top hit"
WHY_TOPIC = "shares topic with top hit"
WHY_REF_OUT = "referenced by top hit"
WHY_DUPLICATE = "duplicate of top hit"


def _resolve(
    settings: Settings,
    store_obj: object,
    factory: callable,
) -> object:
    return store_obj if store_obj is not None else factory()


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
    2. embed .embed([query]) -> 1 vector.
    3. vectors.search(vec, top_n_chunks) -> list[(chunk_id, file_id, vector_score)]
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
    s = settings or Settings()
    vectors = _resolve(s, vectors, lambda: VectorStore(s.store.data_dir / "vectors", s.store.lancedb_table))  # type: ignore[arg-type]
    graph = _resolve(s, graph, lambda: GraphStore(s.store.data_dir / s.store.kuzu_db))  # type: ignore[arg-type]
    embed = _resolve(s, embed, lambda: EmbedClient(s))  # type: ignore[arg-type]
    records = _resolve(s, records, lambda: FileRecordStore(s.store.data_dir / "records"))  # type: ignore[arg-type]

    if not query.strip():
        return []

    # 1. embed + ANN
    qv = embed.embed([query])[0]
    top_n = s.retrieve.top_k_chunks
    raw_hits = vectors.search(qv, k=top_n)  # list[(chunk_id, file_id, score)]
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
        fused.append(FileHit(
            file_id=fid, path=path, summary=summary,
            score=score, why=why,
        ))
    fused.sort(key=lambda h: h.score, reverse=True)
    return fused[:k]
