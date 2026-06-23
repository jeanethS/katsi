"""LanceDB-backed vector store for chunk embeddings."""

from __future__ import annotations

from pathlib import Path

import lancedb
import pyarrow as pa

from mnemo_core.models import Chunk


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
        schema = pa.schema([
            ("id", pa.string()),
            ("file_id", pa.string()),
            ("ordinal", pa.int32()),
            ("text", pa.string()),
            ("vector", pa.list_(pa.float32(), embed_dim)),
            ("token_count", pa.int32()),
        ])
        if self._table_name not in self._db.list_tables():
            self._tbl = self._db.create_table(
                self._table_name, schema=schema, mode="overwrite"
            )
        else:
            self._tbl = self._db.open_table(self._table_name)

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

    def search(self, query_vector: list[float], k: int = 8) -> list[tuple[str, str, float]]:
        """ANN search returning a list of (chunk_id, file_id, score) tuples.

        Score = 1 / (1 + _distance), so higher is better.
        """
        results = self._tbl.search(query_vector).limit(k).to_list()
        out: list[tuple[str, str, float]] = []
        for row in results:
            score = 1.0 / (1.0 + row["_distance"])
            out.append((row["id"], row["file_id"], score))
        return out

    def delete_by_file(self, file_id: str) -> None:
        """Delete all chunks belonging to file_id."""
        self._tbl.delete(f"file_id = '{file_id}'")

    def count(self) -> int:
        """Number of rows in the chunks table."""
        return self._tbl.count_rows()

    def close(self) -> None:
        """Best-effort cleanup (LanceDB has nothing to close; kept for symmetry)."""
        pass
