"""LanceDB-backed vector store for chunk embeddings."""

from __future__ import annotations

from pathlib import Path

import lancedb
import pyarrow as pa

from katsi_core.models import Chunk


class VectorStore:
    """LanceDB-backed store for chunk vectors.

    Stores chunk embeddings with metadata in a LanceDB table.
    The embedding dimension is supplied at init_table time (not hardcoded).
    """

    def __init__(self, db_path: Path, table_name: str = "chunks") -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(db_path))
        self._table_name = table_name
        self._tbl = None

    def init_table(self, embed_dim: int) -> None:
        """Create the chunks table with the fixed schema if it does not exist."""
        schema = pa.schema(
            [
                ("id", pa.string()),
                ("file_id", pa.string()),
                ("ordinal", pa.int32()),
                ("text", pa.string()),
                ("vector", pa.list_(pa.float32(), embed_dim)),
                ("token_count", pa.int32()),
            ]
        )
        if self._table_name not in self._db.list_tables().tables:
            self._tbl = self._db.create_table(self._table_name, schema=schema, mode="overwrite")
        else:
            self._tbl = self._db.open_table(self._table_name)

    def _require_table(self):
        """Open the existing table on demand.

        Read-only callers (search) never go through init_table, so the handle
        is opened lazily here rather than left as None.
        """
        if self._tbl is None:
            if self._table_name not in self._db.list_tables().tables:
                raise RuntimeError(
                    f"vector table {self._table_name!r} does not exist; index files first"
                )
            self._tbl = self._db.open_table(self._table_name)
        return self._tbl

    def upsert_chunks(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        """Add chunks; for each chunk's file_id, delete any existing rows first."""
        if not chunks and not vectors:
            return
        if len(chunks) != len(vectors):
            raise ValueError("len(chunks) != len(vectors)")

        # Collect unique file_ids and delete their existing rows
        file_ids = {c.file_id for c in chunks}
        for fid in file_ids:
            self._tbl.delete(f"file_id = '{fid}'")

        # Build Arrow table
        rows = [
            {
                "id": c.id,
                "file_id": c.file_id,
                "ordinal": c.ordinal,
                "text": c.text,
                "vector": vectors[i],
                "token_count": c.token_count,
            }
            for i, c in enumerate(chunks)
        ]
        schema = self._tbl.schema
        tbl = pa.Table.from_pylist(rows, schema=schema)
        self._tbl.add(tbl)

    def search(self, query_vector: list[float], k: int = 8) -> list[object]:
        """ANN search returning a list of search result objects with file_id, id, and score attributes.

        Score = 1 / (1 + _distance), so higher is better.
        """
        results = self._require_table().search(query_vector).limit(k).to_list()

        class SearchResult:
            def __init__(self, id: str, file_id: str, score: float):
                self.id = id
                self.file_id = file_id
                self.score = score

            def __repr__(self):
                return f"SearchResult(id={self.id!r}, file_id={self.file_id!r}, score={self.score:.3f})"

            def __iter__(self):
                """Allow unpacking as (chunk_id, file_id, score)."""
                return iter((self.id, self.file_id, self.score))

            def __getitem__(self, index: int):
                """Allow index-based access: result[0] -> chunk_id, result[1] -> file_id, result[2] -> score."""
                if index == 0:
                    return self.id
                elif index == 1:
                    return self.file_id
                elif index == 2:
                    return self.score
                else:
                    raise IndexError("SearchResult index out of range")

        out: list[SearchResult] = []
        for row in results:
            score = 1.0 / (1.0 + row["_distance"])
            out.append(SearchResult(row["id"], row["file_id"], score))
        return out

    def delete_by_file(self, file_id: str) -> None:
        """Delete all chunks belonging to file_id."""
        if self._tbl is None:
            if self._table_name not in self._db.list_tables().tables:
                return
            self._tbl = self._db.open_table(self._table_name)
        self._tbl.delete(f"file_id = '{file_id}'")

    def count(self) -> int:
        """Number of rows in the chunks table."""
        if self._tbl is None:
            if self._table_name not in self._db.list_tables().tables:
                return 0
            self._tbl = self._db.open_table(self._table_name)
        return self._tbl.count_rows()

    def rebuild_from_authoritative(
        self,
        chunks: list[tuple[str, str, int, str, int]],  # (chunk_id, file_id, ordinal, text, token_count)
        vectors: list[tuple[str, list[float]]],  # (chunk_id, vector)
    ) -> None:
        """Rebuild the entire vector projection from authoritative resources and cached enrichment.

        This is an idempotent operation that:
        1. Clears all existing vector data
        2. Rebuilds from authoritative chunks (current state)
        3. Uses cached embeddings to avoid redundant LLM calls

        Args:
            chunks: List of (chunk_id, file_id, ordinal, text, token_count) tuples from authoritative resources
            vectors: List of (chunk_id, vector) tuples from cached enrichment
        """
        # Ensure table exists
        if self._tbl is None:
            if self._table_name not in self._db.list_tables().tables:
                raise RuntimeError("Vector table not initialized. Call init_table first.")
            self._tbl = self._db.open_table(self._table_name)

        # Clear existing data idempotently
        if self._table_name in self._db.list_tables().tables:
            self._db.drop_table(self._table_name)

        # Re-create table with original schema
        schema = self._tbl.schema
        self._tbl = self._db.create_table(self._table_name, schema=schema, mode="overwrite")

        # Build lookup for vectors by chunk_id
        vector_lookup = {chunk_id: vec for chunk_id, vec in vectors}

        # Rebuild chunks from authoritative resources with cached embeddings
        if chunks:
            # Collect unique file_ids and prepare data
            rows = []
            for chunk_id, file_id, ordinal, text, token_count in chunks:
                if chunk_id in vector_lookup:
                    rows.append({
                        "id": chunk_id,
                        "file_id": file_id,
                        "ordinal": ordinal,
                        "text": text,
                        "vector": vector_lookup[chunk_id],
                        "token_count": token_count,
                    })

            # Bulk insert all chunks
            if rows:
                tbl = pa.Table.from_pylist(rows, schema=schema)
                self._tbl.add(tbl)

    def close(self) -> None:
        """Best-effort cleanup (LanceDB has nothing to close; kept for symmetry)."""
        pass
