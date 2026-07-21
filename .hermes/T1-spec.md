# T1 — Store adapters (LanceDB + Kùzu)

You are extending the EXISTING katsi uv workspace in the current working directory.
T0 already created the scaffold (packages/, models.py, config.py). Do NOT recreate it.
Only ADD the new files listed below.

## TOOL RULES (read first)

Do NOT explore any codebase.
Do NOT search for anything.
Do NOT call glob, task, doom_loop, or any discovery tool.
Use ONLY your file-write/edit tool and `bash` (for `uv run pytest`, `uv run ruff check .`).
Write each file directly with the exact contents / contracts specified below.

When done, run `uv run pytest -q 2>&1 | tail -25` and `uv run ruff check . 2>&1 | tail -10`
from the project root, and report exit codes plus the tail output.

## 0. API patterns already verified (use these — they work)

### LanceDB (installed 0.33.x)

```python
import lancedb
import pyarrow as pa

db = lancedb.connect(str(data_dir / "vectors"))   # dir created on connect
schema = pa.schema([
    ("id", pa.string()),
    ("file_id", pa.string()),
    ("ordinal", pa.int32()),
    ("text", pa.string()),
    ("vector", pa.list_(pa.float32(), embed_dim)),  # fixed dim
    ("token_count", pa.int32()),
])
tbl = db.create_table("chunks", schema=schema, mode="overwrite")  # if-not-exists: check db.table_names() first
tbl.add(pa.Table.from_pylist(rows_dict, schema=schema))           # append rows
res = tbl.search(query_vector_as_list_of_floats).limit(k).to_list()  # list of dicts, includes "_distance" (lower=better)
tbl.delete("file_id = 'fmark'")                                   # SQL-like predicate string
tbl.count_rows()
```

Result of `to_list()` is a `list[dict]` with the chunk fields PLUS a `_distance` field.

### Kùzu (installed 0.11.x)

```python
import kuzu
db = kuzu.Database(str(data_dir / "graph"))   # dir auto-created
conn = kuzu.Connection(db)
conn.execute("CREATE NODE TABLE IF NOT EXISTS File(id STRING, PRIMARY KEY(id))")
conn.execute("CREATE (f:File {id:'abc', path:'/x', name:'x.md', ext:'.md', summary:'s', mtime:1.5})")  # Cypher: SINGLE quotes for strings
# Parameterized queries are supported:
conn.execute("MERGE (f:File {id:$id}) SET f.summary=$s", {"id":"abc","s":"updated"})
r = conn.execute("MATCH (f:File {id:$id})-[:REFERENCES]->(o:File) RETURN o.id, o.path", {"id":"abc"})
r.has_next()   # bool
r.get_next()   # list of values; each is a Value with .value accessor (or a dict for returned nodes)
# To get the actual scalar: row[i].value when row[i] is a kuzu Value wrapper. For str/int returns the primitive directly in some bindings.
# Pattern that works in 0.11.x: iterate and unwrap to .value only if the value is not a primitive.
```

Helper you must write at top of `graph.py`:

```python
def _unwrap(val):
    """Return the Python value from a kuzu Value, or val itself if already bare."""
    return val.value if hasattr(val, "value") else val
```

## 1. Existing models you can import

From `katsi_core.models`:
- `FileRecord` (id, path, name, ext, mime, size_bytes, mtime, content_hash, status, summary, last_indexed_at, error)
- `Chunk` (id, file_id, ordinal, text, token_count)
- `IndexStatus` (StrEnum: PENDING, INDEXED, STALE, ERROR)

Settings lives at `katsi_core.config.Settings`. `Settings().store.data_dir` gives `~/.katsi` by default;
`Settings().store.lancedb_table` is `"chunks"` and `Settings().store.kuzu_db` is `"graph"`.

## 2. Files to create (5 new files)

```
packages/core/katsi_core/store/__init__.py
packages/core/katsi_core/store/vectors.py
packages/core/katsi_core/store/graph.py
tests/test_vectors.py
tests/test_graph.py
```

## 3. Contract: `packages/core/katsi_core/store/__init__.py`

```python
"""katsi storage adapters."""
```

That's it — keeps the directory a package.

## 4. Contract: `packages/core/katsi_core/store/vectors.py`

Class `VectorStore`. LanceDB-backed. Constructor params and methods below.
Tests must use temp dirs and NO network.

```python
class VectorStore:
    def __init__(self, db_path: Path, table_name: str = "chunks") -> None: ...
    def init_table(self, embed_dim: int) -> None:
        """Create the chunks table with the fixed schema from §0 if it does not
        already exist in db.table_names(). Idempotent."""
    def upsert_chunks(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        """Add chunks; for each chunk's file_id, delete any existing rows first
        (so re-index is a clean replace). Vectors length must match chunks length."""
    def search(self, query_vector: list[float], k: int = 8) -> list[tuple[str, str, float]]:
        """ANN search returning a list of (chunk_id, file_id, score) tuples.
        score = similarity = (1 / (1 + _distance)). Sorted descending by score."""
    def delete_by_file(self, file_id: str) -> None:
        """Delete all chunks belonging to file_id."""
    def count(self) -> int:
        """Number of rows in the chunks table."""
    def close(self) -> None:
        """Best-effort cleanup (LanceDB has nothing to close; keep method for symmetry)."""
```

Schema columns: `id` str, `file_id` str, `ordinal` int32, `text` str,
`vector` `list_(pa.float32(), embed_dim)`, `token_count` int32 — exactly as §0.

Notes:
- `init_table` must accept the embedding dimension at runtime (the embed model's dim is
  only known after the first call to Ollama — do NOT hardcode it).
- `upsert_chunks` builds an Arrow table from the passed `Chunk` objects + vectors and
  calls `tbl.add(...)`. First deletes rows where `file_id` matches using `tbl.delete(...)`.
- The `~` tilde in `Path.home() / ".katsi"` is already expanded; do not expand again.
- LanceDB stores its data under "db_path"; create parent dir if missing.

Notes about upsert_with_chunks-method semantics:
- If `chunks` and `vectors` are empty, return immediately (no-op).
- Match lengths: raise `ValueError("len(chunks) != len(vectors)")` on mismatch.

## 5. Contract: `packages/core/katsi_core/store/graph.py`

Kùzu-backed. Implements the DDL from the architecture spec §5.2 exactly. Add an unwrapping
helper (`_unwrap`) at module top — see §0.

```python
def _unwrap(val):
    """Unwrap a kuzu Value to its bare Python value, or return val if already bare."""
    return val.value if hasattr(val, "value") else val


class GraphStore:
    def __init__(self, db_path: Path) -> None:
        """Create parent dirs, open the kuzu Database, init schema."""

    def init_schema(self) -> None:
        """Run the DDL idempotently (IF NOT EXISTS) from spec §5.2. Safe to call twice:
        init_schema().init_schema() must not raise."""

    def upsert_file(self, file: FileRecord) -> None:
        """MERGE the File node by id; set path, name, ext, summary (use "" if None),
        mtime. Use idempotent MERGE - {id: $id} pattern."""

    def upsert_entity(self, name: str, kind: str) -> None:
        """MERGE the Entity node by name, set kind."""

    def upsert_topic(self, name: str) -> None:
        """MERGE the Topic node by name."""

    def add_mentions(self, file_id: str, entities: list[dict], weight: float = 1.0) -> None:
        """For each {name, kind} entity in @entities: upsert entity, then
        MATCH the file and entity and MERGE (f)-[:MENTIONS {weight}]->(e)."""

    def add_about(self, file_id: str, topics: list[str], weight: float = 1.0) -> None:
        """For each topic: upsert_topic, then MERGE (f)-[:ABOUT {weight}]->(t)."""

    def add_reference(self, src_file_id: str, dst_file_id: str) -> None:
        """MATCH both File nodes, MERGE (src)-[:REFERENCES]->(dst).
        If dst doesn't exist, skip silently (refs may point at un-indexed files)."""

    def add_duplicate(self, src_file_id: str, dst_file_id: str, similarity: float) -> None:
        """MATCH both, MERGE (src)-[:DUPLICATE_OF {similarity}]->(dst)."""

    def neighbors(self, file_id: str, hops: int = 1) -> list[dict]:
        """Return 1-hop neighbors. For hops=1, return a list of dicts:
        [{"file_id": "x", "via": "references" | "mentioned-entity" | "shared-topic" |
                                 "duplicate"}, "name": optional (the shared entity/topic name), "score": float}]
        Implement using three explicit MATCH queries (one per relationship kind):
        a) REFERENCES: (f)-[:REFERENCES]->(o:File) — via="references"
        b) MENTIONS shared entity: (f)-[:MENTIONS]->(e)<-[:MENTIONS]-(o:File) — via="mentioned-entity", name=entity name
        c) ABOUT shared topic: (f)-[:ABOUT]->(t)<-[:ABOUT]-(o:File) — via="shared-topic", name=topic name
        d) DUPLICATE_OF: (f)-[:DUPLICATE_OF {similarity}]->(o:File) — via="duplicate", score=similarity
        Always filter WHERE o.id <> $id. hops>1 is NOT needed for v0.1 (raise
        NotImplementedError if hops != 1).
        """

    def get_file(self, file_id: str) -> FileRecord | None:
        """MATCH (f:File {id:$id}) RETURN f; return a FileRecord or None.
        If row found, the node is returned as a dict — feed dict into FileRecord(**dict).
        Note: the Kùzu node dict has a subset of FileRecord fields; fill the missing ones
        with defaults (mime='', size_bytes=0, content_hash='', mtime from node, status=INDEXED)."""

    def delete_by_file(self, file_id: str) -> None:
        """Best-effort: delete the File node. The file's edges should be deleted via
        DETACH DELETE in a single statement: MATCH (f:File {id:$id}) DETACH DELETE f."""
```

Constructor implementation reference (use this — verified to work):

```python
def __init__(self, db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    self._db = kuzu.Database(str(db_path))
    self._conn = kuzu.Connection(self._db)
    self.init_schema()
```

## 6. Contract: `tests/test_vectors.py`

Must use `tmp_path` and NO network. Coverage:

- `test_init_creates_table` — new VectorStore with fresh tmp_path; calling init_table(8)
  then count() returns 0 and a second init_table(8) is a no-op.
- `test_upsert_and_search` — insert 2 chunks for file "f1"; search with one of the
  inserted vectors returns that chunk first.
- `test_upsert_replaces_by_file_id` — upsert chunks for "f1", then upsert different
  chunks for "f1"; count() still equals the latest set length (old ones replaced).
- `test_delete_by_file` — insert 2 chunks for "f1"; delete_by_file("f1"); count() == 0.
- `test_search_returns_three_tuple` — every result is a (str, str, float) tuple with
  file_id == the inserted file_id.
- `test_empty_upsert_is_noop` — upsert_chunks([], []) does not raise.

Use random或者其他 vector values; the choice of dimension doesn't matter as long as
init/search consistency holds. Use deterministic embeddings.

## 7. Contract: `tests/test_graph.py`

Must use `tmp_path`. Coverage:

- `test_schema_init_idempotent` — GraphStore(tmp_path).init_schema() called twice does not raise.
- `test_upsert_and_get_file` — create FileRecord, upsert_file; get_file(id) returns
  a FileRecord with the same path/summary/last fields.
- `test_get_missing_file_returns_none` — get_file("nope") is None.
- `test_mentions_and_peers` — file f1 MENTIONS entity Acme; file f2 also MENTIONS Acme;
  neighbors(f1) returns [{"file_id":"f2","via":"mentioned-entity","name":"Acme","score":?}].
- `test_references` — file f1 REFERENCES f2; neighbors(f1) returns
  [{"file_id":"f2","via":"references",...}].
- `test_about_shared_topic` — similar to mentions for Topic.
- `test_delete_by_file_removes_node` — upsert f1; upsert MENTIONS edge to Acme; delete_by_file("f1");
  File f1 is gone (get_file returns None). Edges may or may not cascade — DETACH DELETE
  ensures both File node and any incoming/outgoing edges for that node drop.
- `test_neighbors_hops_other_than_1_raises` — neighbors("f1", hops=2) raises NotImplementedError.

## 8. After writing the 5 files

Run from project root:

```bash
uv run pytest -q 2>&1 | tail -25
uv run ruff check . 2>&1 | tail -10
```

Both must exit 0. Paste the tail outputs in your final report.

## 9. Constraints / anti-patterns

- Do NOT add new dependencies to pyproject.toml. lancedb, kuzu are already in katsi-core deps.
- Do NOT modify files in `packages/core/katsi_core/models.py`, `config.py`, or
  `__init__.py` from T0. Do NOT touch mcp_server/ or cli/.
- Do NOT call any external service (no real Ollama, no real network). All tests use tmp dirs.
- Do NOT leave TODO comments anywhere.

## 10. Done when

- All 5 files exist with the contracts above.
- `uv run pytest` passes (including all existing T0 smoke tests).
- `uv run ruff check .` is clean.
- Hand back a short report listing files created + final pytest/ruff status.
