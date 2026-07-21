from __future__ import annotations

from katsi_core.clients.embed import EmbedClient
from katsi_core.config import Settings
from katsi_core.ingest.records import FileRecordStore
from katsi_core.models import Evidence, FileHit
from katsi_core.retrieve.scoring import (
    duplicate_evidence,
    entity_evidence,
    hop_evidence,
    rank_hits,
    reference_evidence,
    render_why,
    score_file,
    topic_evidence,
    vector_evidence,
)
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore


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
       to get peer file_ids + their via + name + weight fields.
    6. Group neighbor connectors by peer file_id.
    7. For each candidate file, collect evidence list via scoring.py builders,
       call score_file() for final score and render_why() for why string.
    8. Use rank_hits() for deterministic sort; slice top k.
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

    # 3. graph expand + group connectors per peer file
    neighbor_data: dict[str, dict] = {}
    for src_fid in top_files:
        for nb in graph.neighbors(src_fid, hops=1):
            peer = nb.get("file_id")
            if not peer or peer == src_fid:
                continue
            data = neighbor_data.setdefault(peer, {})
            via = nb.get("via", "")
            if via == "mentioned-entity":
                data.setdefault("MENTIONS", []).append((nb["name"], nb["weight"]))
            elif via == "shared-topic":
                data.setdefault("ABOUT", []).append((nb["name"], nb["weight"]))
            elif via == "references":
                data["REFERENCES"] = data.get("REFERENCES", 0) + 1
            elif via == "duplicate":
                data["DUPLICATE_OF"] = nb["score"]

    # 4. score fusion via evidence
    weights = s.retrieve.weights
    candidate_files = set(top_files) | set(neighbor_data.keys())
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

        evidence: list[Evidence] = []
        nbd = neighbor_data.get(fid, {})

        vec = vec_best.get(fid, 0.0)
        ev = vector_evidence(vec, weights)
        if ev:
            evidence.append(ev)

        mentions = nbd.get("MENTIONS")
        if mentions:
            ev = entity_evidence(mentions, weights)
            if ev:
                evidence.append(ev)

        about = nbd.get("ABOUT")
        if about:
            ev = topic_evidence(about, weights)
            if ev:
                evidence.append(ev)

        if nbd.get("REFERENCES", 0) > 0:
            ev = reference_evidence("out", weights)
            if ev:
                evidence.append(ev)

        dup = nbd.get("DUPLICATE_OF")
        if dup is not None:
            ev = duplicate_evidence(dup, weights)
            if ev:
                evidence.append(ev)

        if fid not in vec_best:
            ev = hop_evidence(1, weights)
            if ev:
                evidence.append(ev)

        score = score_file(evidence, weights)
        why = render_why(evidence)
        fused.append(FileHit(
            file_id=fid, path=path, summary=summary,
            score=score, why=why, evidence=evidence,
        ))

    fused = rank_hits(fused)
    return fused[:k]
