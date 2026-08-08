from __future__ import annotations

import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

import blake3

from katsi_core.clients.embed import EmbedClient
from katsi_core.clients.llm import ExtractionError, LLMClient
from katsi_core.config import Settings
from katsi_core.ingest.chunk import chunk
from katsi_core.ingest.enrich import apply_extraction
from katsi_core.ingest.extract import extract_text
from katsi_core.ingest.records import FileRecordStore
from katsi_core.models import FileRecord, IndexStatus
from katsi_core.store.enrichment_cache import EnrichmentCache
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore
from katsi_core.workspace.enrichment import EnrichmentFingerprint
from katsi_core.workspace.extraction_gate import ExtractionGate

logger = logging.getLogger(__name__)


class IngestPipeline:
    """End-to-end per-file ingest. §7.1 of the architecture spec."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        graph: GraphStore | None = None,
        vectors: VectorStore | None = None,
        embed: EmbedClient | None = None,
        llm: LLMClient | None = None,
        records: FileRecordStore | None = None,
        enrichment_cache: EnrichmentCache | None = None,
    ) -> None:
        """
        Lazily construct any of graph/vectors/embed/llm/records from settings
        (or overrides) on first use. This makes the pipeline cheap to construct
        for tests, and lets the test inject fakes for embed+llm to assert the
        no-work path on second calls.
        """
        self._settings = settings or Settings()
        self._graph = graph
        self._vectors = vectors
        self._embed = embed
        self._llm = llm
        self._records = records
        self._enrichment_cache = enrichment_cache
        self._vectors_inited = False

    # --- lazy accessors ---

    def _graph_store(self) -> GraphStore:
        if self._graph is None:
            self._graph = GraphStore(self._settings.store.data_dir / self._settings.store.kuzu_db)
        return self._graph

    def _vector_store(self) -> VectorStore:
        if self._vectors is None:
            self._vectors = VectorStore(
                self._settings.store.data_dir / "vectors",
                self._settings.store.lancedb_table,
            )
        return self._vectors

    def _embed_client(self) -> EmbedClient:
        if self._embed is None:
            self._embed = EmbedClient(self._settings)
        return self._embed

    def _llm_client(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(self._settings)
        return self._llm

    def _record_store(self) -> FileRecordStore:
        if self._records is None:
            self._records = FileRecordStore(self._settings.store.data_dir / "records")
        return self._records

    # --- main entry ---

    def index_file(self, path: Path) -> FileRecord:
        """Per §7.1:
        1. blake3(realpath) -> file_id, blake3(bytes) -> content_hash.
        2. If existing FileRecord with same content_hash AND status==INDEXED,
           return it -- DO NOT embed/LLM/chat (the saver).
        3. extract_text -> "" means ERROR (set status, persist, return).
        4. chunk(file_id, text) -> list[Chunk]
        5. embed.embed(chunk texts) -> vectors -> vectors.upsert_chunks
        6. llm.extract(text) -> Extraction (catch ExtractionError -> mark ERROR,
           do NOT poison graph; persist record; return).
        7. apply_extraction(file_record, extraction, graph) -- File + edges
        8. record.summary = extraction.summary; status = INDEXED; last_indexed_at = now.
        9. records.put(record). Return record.
        """
        p = Path(path).resolve()  # realpath
        file_id = blake3.blake3(str(p).encode("utf-8")).hexdigest()
        try:
            file_bytes = p.read_bytes()
            content_hash = blake3.blake3(file_bytes).hexdigest()
        except OSError as e:
            logger.warning("index_file: cannot read %s: %r", p, e)
            record = FileRecord(
                id=file_id,
                path=str(p),
                name=p.name,
                ext=p.suffix.lower(),
                mime="",
                size_bytes=0,
                mtime=0.0,
                content_hash="",
                status=IndexStatus.ERROR,
                error=f"read error: {e}",
            )
            self._record_store().put(record)
            return record

        stat = p.stat()
        ext = p.suffix.lower()
        mime, _ = mimetypes.guess_type(str(p))
        record_template = {
            "id": file_id,
            "path": str(p),
            "name": p.name,
            "ext": ext,
            "mime": mime or "",
            "size_bytes": stat.st_size,
            "mtime": stat.st_mtime,
            "content_hash": content_hash,
        }

        # ---- saver: skip-if-unchanged ---------------------------------
        existing = self._record_store().get(file_id)
        if (
            existing is not None
            and existing.content_hash == content_hash
            and existing.status == IndexStatus.INDEXED
        ):
            logger.info("index_file: skip unchanged %s", p)
            return existing

        # ---- extract text ----------------------------------------------
        text = extract_text(p)
        if not text:
            self._delete_current_projections(file_id)
            record = FileRecord(
                **record_template,
                status=IndexStatus.ERROR,
                error="extraction returned empty text",
            )
            self._record_store().put(record)
            return record

        # ---- chunk -----------------------------------------------------
        chunks = chunk(
            file_id,
            text,
            target_tokens=self._settings.ingest.chunk_token_target,
            overlap=self._settings.ingest.chunk_token_overlap,
        )
        if not chunks:
            self._delete_current_projections(file_id)
            record = FileRecord(
                **record_template,
                status=IndexStatus.ERROR,
                error="chunker produced zero chunks",
            )
            self._record_store().put(record)
            return record

        # ---- summarize-once + strict extraction -----------------------
        fingerprint = EnrichmentFingerprint(
            content_hash=content_hash,
            extraction_contract_version=self._settings.workspace.enrichment.extraction_contract_version,
            model_identity=self._settings.ollama.llm_model,
            prompt_version=self._settings.workspace.enrichment.prompt_version,
            chunking_version=self._settings.workspace.enrichment.chunking_version,
            semantic_settings_version=self._settings.workspace.enrichment.semantic_settings_version,
        )
        try:
            cached = self._enrichment_cache.get(fingerprint) if self._enrichment_cache else None
            raw_extraction = cached or self._llm_client().extract(text).model_dump()
            strict_extraction = ExtractionGate().validate(lambda: raw_extraction)
            if cached is None and self._enrichment_cache is not None:
                self._enrichment_cache.put(fingerprint, strict_extraction.model_dump())
            extraction = strict_extraction
        except Exception as error:
            if self._enrichment_cache is not None:
                self._enrichment_cache.put_error(fingerprint, str(error))
            if not isinstance(error, ExtractionError):
                logger.warning("index_file: extraction validation failed for %s: %r", p, error)
            else:
                logger.warning("index_file: extraction failed for %s: %r", p, error)
            self._delete_current_projections(file_id)
            record = FileRecord(
                **record_template,
                status=IndexStatus.ERROR,
                error=f"extraction error: {error}",
                summary=None,
            )
            self._record_store().put(record)
            return record

        # ---- embed + vector upsert ------------------------------------
        try:
            embed_client = self._embed_client()
            vs = self._vector_store()
            if not self._vectors_inited:
                vs.init_table(embed_client.dim)
                self._vectors_inited = True
            chunk_texts = [c.text for c in chunks]
            vectors = embed_client.embed(chunk_texts)
            vs.upsert_chunks(chunks, vectors)
        except Exception as error:
            logger.warning("index_file: embed/vector upsert failed for %s: %r", p, error)
            record = FileRecord(
                **record_template,
                status=IndexStatus.ERROR,
                error=f"embed/vector failure: {error}",
                summary=None,
            )
            self._record_store().put(record)
            return record

        # ---- graph writes ---------------------------------------------
        record = FileRecord(
            **record_template,
            status=IndexStatus.INDEXED,
            summary=extraction.summary,
            last_indexed_at=datetime.now(timezone.utc),  # noqa: UP017
        )
        try:
            apply_extraction(record, extraction, self._graph_store())
        except Exception as e:
            # graph write failure does NOT roll back the success of
            # extraction; downgrade to STALE so the next run retries.
            logger.warning("index_file: graph enrich failed for %s: %r", p, e)
            record = record.model_copy(
                update={"status": IndexStatus.STALE, "error": f"graph error: {e}"}
            )

        # ---- persist --------------------------------------------------
        self._record_store().put(record)
        logger.info(
            "index_file: indexed %s (%d chunks, status=%s)",
            p,
            len(chunks),
            record.status,
        )
        return record

    def _delete_current_projections(self, file_id: str) -> None:
        """Remove stale current graph/vector data after terminal ingestion failure."""
        try:
            self._vector_store().delete_by_file(file_id)
            self._graph_store().delete_by_file(file_id)
        except Exception as error:
            logger.warning(
                "index_file: failed to remove stale projections for %s: %r", file_id, error
            )
