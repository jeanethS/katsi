# T4 — Ingest pipeline (the saver path)

Extends the existing mnemo workspace. T0/T1/T2/T3 already done — add only the new files.

## TOOL RULES (read first)

Do NOT explore any codebase. Do NOT search for anything.
Use ONLY your file-write/edit tool and `bash` (for `uv run pytest`, `uv run ruff check .`).

When done run `uv run pytest -q 2>&1 | tail -20` and `uv run ruff check . 2>&1 | tail -10`
from the project root, report exit codes + tail output.

## 0. Why a new FileRecordStore

T1's graph.py File node schema is deliberately small (id, path, name, ext, summary,
mtime). It does not store `content_hash`, `status`, `last_indexed_at`, `error`, etc.
But §7.1 of the architecture spec requires checking the content hash before
re-summarizing ("the saver"). So T4 introduces a tiny JSON-backed FileRecordStore.
Keep it simple — no extra deps.

## 1. Existing pieces you wire together

From `mnemo_core.models`: `FileRecord`, `Chunk`, `Extraction`, `IndexStatus`.
From `mnemo_core.config`: `Settings`.
From `mnemo_core.store.graph`: `GraphStore`.
From `mnemo_core.store.vectors`: `VectorStore`.
From `mnemo_core.clients.embed`: `EmbedClient`.
From `mnemo_core.clients.llm`: `LLMClient`, `ExtractionError`.
From `mnemo_core.ingest.extract`: `extract_text`.
From `mnemo_core.ingest.chunk`: `chunk`.

blake3 is in mnemo-core deps:
```python
import blake3
h = blake3.blake3(some_bytes)
hex = h.hexdigest()
```

mimetypes from stdlib for mime type guess.

## 2. Files to create (5 new files)

```
packages/core/mnemo_core/ingest/records.py
packages/core/mnemo_core/ingest/enrich.py
packages/core/mnemo_core/ingest/pipeline.py
tests/test_enrich.py
tests/test_pipeline.py
```

Do NOT modify the existing `ingest/__init__.py`, `extract.py`, or `chunk.py`.

## 3. Contract: `packages/core/mnemo_core/ingest/records.py`

A tiny JSON-backed FileRecord store. Atomic-ish writes (write to .tmp + os.replace).

```python
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from mnemo_core.models import FileRecord

logger = logging.getLogger(__name__)


class FileRecordStore:
    """JSON-on-disk store for FileRecords, keyed by file id.

    Plain dict {id: FileRecord.model_dump(mode='json')} serialized to one file.
    Suitable for v0.1 — not for huge trees. Test-friendly: tmp_path works."""

    def __init__(self, data_dir: Path) -> None:
        """data_dir is the directory; the file is data_dir / 'file_records.json'."""
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._data_dir / "file_records.json"
        self._cache: dict[str, dict] | None = None

    def _load(self) -> dict[str, dict]:
        if self._cache is not None:
            return self._cache
        if not self._path.exists():
            self._cache = {}
            return self._cache
        try:
            with open(self._path, encoding="utf-8") as f:
                self._cache = json.load(f)
            if not isinstance(self._cache, dict):
                self._cache = {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("FileRecordStore: corrupt file %s: %r", self._path, e)
            self._cache = {}
        return self._cache

    def _flush(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._cache, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self._path)

    def get(self, file_id: str) -> FileRecord | None:
        rec = self._load().get(file_id)
        if rec is None:
            return None
        try:
            return FileRecord(**rec)
        except Exception as e:
            logger.warning("FileRecordStore.get: bad record %s: %r", file_id, e)
            return None

    def put(self, record: FileRecord) -> None:
        data = self._load()
        data[record.id] = record.model_dump(mode="json")
        self._flush()

    def delete(self, file_id: str) -> None:
        data = self._load()
        if file_id in data:
            data.pop(file_id)
            self._flush()

    def list_all(self) -> list[FileRecord]:
        return [FileRecord(**v) for v in self._load().values()]

    def count_by_status(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self._load().values():
            st = v.get("status", "pending")
            out[st] = out.get(st, 0) + 1
        return out
```

## 4. Contract: `packages/core/mnemo_core/ingest/enrich.py`

Maps an `Extraction` JSON-LLM result to graph writes.

```python
from __future__ import annotations

import logging

from mnemo_core.models import FileRecord
from mnemo_core.store.graph import GraphStore
from mnemo_core.models import Extraction  # noqa: F401  (used in type hint below)

logger = logging.getLogger(__name__)


def apply_extraction(
    file_record: FileRecord,
    extraction: Extraction,
    graph: GraphStore,
) -> None:
    """Push the Extraction result into the graph.

    Order matters for idempotency:
      1. upsert_file(file_record)         — File node with summary etc.
      2. add_mentions(file_id, entities) — Entity nodes + MENTIONS edges
      3. add_about(file_id, topics)       — Topic nodes + ABOUT edges
      4. for ref in extraction.references:
             resolved = try_resolve(ref, file_record.path)
             if resolved is not None (an indexed file id): add_reference(...)
             else: skip silently (the destination may not be indexed yet)
    The 'why': ref strings from the LLM are filename hints, not file ids.
    A reference is resolvable if it matches another File .name or is a suffix
    of another File .path in the graph — for v0.1 keep this simple: just call
    graph.add_reference(file_id, target_id) ONLY when target exists; otherwise
    skip. Implementation may simply iterate references names and look up
    File nodes whose name matches — for v0.1 you can also just SKIP reference
    resolution entirely and rely on T5's retrieval to surface relationships if
    names match in future; here only MENTIONS and ABOUT are wired.

    Concretely for v0.1 implementation (do exactly this):
      - always: upsert_file, add_mentions(file_id, extraction.entities),
        add_about(file_id, extraction.topics).
      - references: try `MATCH (o:File {name:$name}) RETURN o.id` for each
        reference's basename; if found, call graph.add_reference(file_id, found_id).
        Use a private helper _resolve_reference(graph, ref) -> Optional[str]
        that does this lookup via graph._conn (or a new method you add to
        GraphStore — but do NOT modify graph.py, use graph._conn directly).
    """
```

Implementation reference you can use:

```python
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def _basename(ref: str) -> str:
    # strip whitespace + path separators
    return os.path.basename(ref.strip().rstrip("/\\").strip())


def _resolve_reference(graph: GraphStore, ref: str) -> str | None:
    base = _basename(ref)
    if not base:
        return None
    try:
        # graph._conn is a private Kuzu connection; ok to use internally
        res = graph._conn.execute(
            "MATCH (o:File {name:$name}) RETURN o.id",
            {"name": base},
        )
        if res.has_next():
            row = res.get_next()
            val = row[0]
            return val.value if hasattr(val, "value") else val
    except Exception as e:
        logger.warning("enrich._resolve_reference: lookup failed for %r: %r", ref, e)
    return None


def apply_extraction(file_record, extraction, graph):
    graph.upsert_file(file_record)
    if extraction.entities:
        graph.add_mentions(file_record.id, extraction.entities)
    if extraction.topics:
        graph.add_about(file_record.id, extraction.topics)
    if extraction.references:
        for ref in extraction.references:
            target_id = _resolve_reference(graph, ref)
            if target_id is not None and target_id != file_record.id:
                try:
                    graph.add_reference(file_record.id, target_id)
                except Exception as e:
                    logger.debug("add_reference %s->%s failed: %r",
                                 file_record.id, target_id, e)
```

## 5. Contract: `packages/core/mnemo_core/ingest/pipeline.py`

```python
from __future__ import annotations

import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

import blake3

from mnemo_core.clients.embed import EmbedClient
from mnemo_core.clients.llm import ExtractionError, LLMClient
from mnemo_core.config import Settings
from mnemo_core.ingest.chunk import chunk
from mnemo_core.ingest.enrich import apply_extraction
from mnemo_core.ingest.extract import extract_text
from mnemo_core.ingest.records import FileRecordStore
from mnemo_core.models import FileRecord, IndexStatus
from mnemo_core.store.graph import GraphStore
from mnemo_core.store.vectors import VectorStore

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
        self._vectors_inited = False

    # --- lazy accessors ---

    def _graph_store(self) -> GraphStore:
        if self._graph is None:
            self._graph = GraphStore(self._settings.store.data_dir / self._settings.store.kuzu_db)
        return self._graph

    def _vector_store(self) -> VectorStore:
        if self._vectors is None:
            self._vectors = VectorStore(self._settings.store.data_dir / "vectors",
                                        self._settings.store.lancedb_table)
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
        1. blake3(realpath) → file_id, blake3(bytes) → content_hash.
        2. If existing FileRecord with same content_hash AND status==INDEXED,
           return it — DO NOT embed/LLM/chat (the saver).
        3. extract_text → "" means ERROR (set status, persist, return).
        4. chunk(file_id, text) → list[Chunk]
        5. embed.embed(chunk texts) → vectors → vectors.upsert_chunks
        6. llm.extract(text) → Extraction (catch ExtractionError → mark ERROR,
           do NOT poison graph; persist record; return).
        7. apply_extraction(file_record, extraction, graph) — File + edges
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
                id=file_id, path=str(p), name=p.name, ext=p.suffix.lower(),
                mime="", size_bytes=0, mtime=0.0, content_hash="",
                status=IndexStatus.ERROR, error=f"read error: {e}",
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
        if existing is not None and existing.content_hash == content_hash \
                and existing.status == IndexStatus.INDEXED:
            logger.info("index_file: skip unchanged %s", p)
            return existing

        # ---- extract text ----------------------------------------------
        text = extract_text(p)
        if not text:
            record = FileRecord(**record_template, status=IndexStatus.ERROR,
                               error="extraction returned empty text")
            self._record_store().put(record)
            return record

        # ---- chunk -----------------------------------------------------
        chunks = chunk(file_id, text,
                       target_tokens=self._settings.ingest.chunk_token_target,
                       overlap=self._settings.ingest.chunk_token_overlap)
        if not chunks:
            record = FileRecord(**record_template, status=IndexStatus.ERROR,
                               error="chunker produced zero chunks")
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
        except Exception as e:
            logger.warning("index_file: embed/vector upsert failed for %s: %r", p, e)
            record = FileRecord(**record_template, status=IndexStatus.ERROR,
                               error=f"embed/vector failure: {e}",
                               summary=None)
            self._record_store().put(record)
            return record

        # ---- summarize-once + extract entities -------------------------
        try:
            extraction = self._llm_client().extract(text)
        except ExtractionError as e:
            logger.warning("index_file: extraction failed for %s: %r", p, e)
            record = FileRecord(**record_template, status=IndexStatus.ERROR,
                               error=f"extraction error: {e}", summary=None)
            # Still keep the chunks we just upserted in the vector store.
            self._record_store().put(record)
            return record

        # ---- graph writes ---------------------------------------------
        record = FileRecord(
            **record_template,
            status=IndexStatus.INDEXED,
            summary=extraction.summary,
            last_indexed_at=datetime.now(timezone.utc),
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
        logger.info("index_file: indexed %s (%d chunks, status=%s)",
                    p, len(chunks), record.status)
        return record
```

Follow this contract. The implementation reference is the source of truth — use it
verbatim; adjust imports as needed.

## 6. Contract: `tests/test_enrich.py`

Minimum 4 tests, using tmp_path + a real GraphStore + a real FileRecordStore (no
network; no embed/LLM ever called here — enrich doesn't touch them):

- `test_apply_extraction_creates_entities_and_topics` — file F1 with summary,
  entities [{Acme, org}], topics ['ai'] — after apply_extraction, GraphStore has
  the Entity + Topic nodes; neighbors via MENTIONS shows them.
- `test_apply_extraction_resolves_reference_by_name` — file F1 references "y.md";
  pre-insert file F2 with name "y.md"; apply_extraction adds an REFERENCES edge
  F1->F2.
- `test_apply_extraction_skips_unresolvable_reference` — reference to
  "nonexistent.md" → enriched without crashing; no edge added (DONE in §5 —
  the helper just skips).
- `test_apply_extraction_idempotent` — call apply_extraction twice with the
  same args; GraphStore has the same nodes (counts unchanged on second call).

## 7. Contract: `tests/test_pipeline.py`

This is THE CRITICAL TEST — verifies the saver (zero embed/LLM on second call).

Use Fake embed/llm clients that count calls:

```python
from types import SimpleNamespace
from mnemo_core.ingest.pipeline import IngestPipeline
from mnemo_core.ingest.records import FileRecordStore
from mnemo_core.models import Extraction, IndexStatus
from mnemo_core.store.graph import GraphStore
from mnemo_core.store.vectors import VectorStore


class _FakeEmbed:
    """Counts every call to embed()."""
    def __init__(self, dim: int = 8):
        self.dim = dim
        self.embed_call_count = 0

    def embed(self, texts):
        self.embed_call_count += 1
        return [[0.5] * self.dim for _ in texts]


class _FakeOllama:
    def __init__(self, content: str):
        self.message = SimpleNamespace(content=content)


class _FakeLLM:
    def __init__(self, json_str: str):
        self.json_str = json_str
        self.extract_call_count = 0

    def extract(self, text, *, attempts=2):
        self.extract_call_count += 1
        # parse same as LLMClient would
        import json
        d = json.loads(self.json_str)
        return Extraction(**d)


def make_pipeline(tmp_path, embed, llm):
    s = GraphStore(tmp_path / "graph")
    v = VectorStore(tmp_path / "vectors")
    r = FileRecordStore(tmp_path / "records")
    p = IngestPipeline(
        settings=None,
        graph=s, vectors=v, embed=embed, llm=llm, records=r,
    )
    return p, s, v, r

EXTRACTION_JSON = '{"summary":"doc summary","entities":[{"name":"Acme","kind":"org"}],"topics":["ai"],"references":[]}'
```

Tests:

- `test_index_file_processes_a_markdown_file` — write `tmp_path/"x.md"` with markdown
  content; index_file(x) returns FileRecord with status=INDEXED, summary="doc summary",
  last_indexed_at not None; embed.embed_call_count == 1; llm.extract_call_count == 1;
  graph.upsert_file already populated (use s.get_file(file_id) — note: graph.get_file
  returns None or a FileRecord; assert .summary == "doc summary").
- `test_second_call_skips_when_unchanged` — same file, same content; call index_file
  twice. On the second call: embed.embed_call_count stays at 1 (no additional embed call),
  llm.extract_call_count stays at 1. The second returned record's status is still INDEXED.
  This is THE saver test.
- `test_index_file_marks_error_on_empty_text` — write `tmp_path/"empty.md"` with no
  bytes; index_file returns status=ERROR; embed.embed_call_count == 0; llm.extract_call_count == 0.
- `test_index_file_marks_stale_or_error_on_extraction_failure` — use a _FakeLLM that
  raises ExtractionError; index_file should return status=ERROR with error set,
  embed_call_count == 1 (still embedded), extract_call_count == 1.
- `test_index_file_reindexes_when_content_changes` — write file with version A; index.
  Modify file contents; index again. embed_call_count == 2, extract_call_count == 2,
  the record's content_hash differs from the previous one.
- `test_record_store_persists_across_pipeline_instances` — index via pipeline1 with
  same records dir; create a new pipeline2 reading from same records dir; pipeline2's
  index_file is the saver (skip unchanged) WITHOUT re-reading the file's bytes from tmp_path modification times. Use file_record_store.get(file_id) directly to verify.

For `_FakeLLM` raising ExtractionError:
```python
class _FakeLLMError:
    def __init__(self):
        self.extract_call_count = 0
    def extract(self, text, *, attempts=2):
        self.extract_call_count += 1
        from mnemo_core.clients.llm import ExtractionError
        raise ExtractionError("fake failure")
```

Note about settings: to prevent IngestPipeline() from constructing a Settings
that requires ~/.mnemo or hits network, explicitly pass `settings=None` will try
Settings() which reads TOML/env — that may pick up `.mnemo.toml` in cwd if any
exists; in tests with tmp_path cwd there is no such file, so the defaults are
used and GraphStore/VectorStore/FileRecordStore get tmp_path-rooted dirs. Pass
explicit graph/vectors/records via the constructor so settings is irrelevant for
those — only embed/llm use ollama.host/url which we override with the fake.

## 8. Constraints

- Do NOT add new dependencies.
- Do NOT modify graph.py, vectors.py, embed.py, llm.py, extract.py, chunk.py
  from earlier tasks.
- Do NOT leave TODO comments.
- The pipeline must tolerate a not-yet-initialized VectorStore (init in
  index_file on first call using embed.dim).
- The saver test is non-negotiable: tests/test_pipeline.py::test_second_call_skips_when_unchanged
  must pass with embed_call_count==1 and extract_call_count==1 after two calls.

## 9. Done when

- All 5 files exist with the contracts above.
- `uv run pytest` passes (existing 43 tests + 4 enrich + 6 pipeline = ~53+).
- `uv run ruff check .` is clean.
- The saver test (test_second_call_skips_when_unchanged) is in the suite
  and passes.
- Hand back a short report.
