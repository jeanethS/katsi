"""LanceDB-backed vector store for chunk embeddings."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import lancedb
import pyarrow as pa

from katsi_core.media.contracts import (
    DerivedRepresentation,
    MediaRepresentationKind,
    MediaRepresentationStatus,
)
from katsi_core.models import Chunk

_TEXTUAL_MEDIA_KINDS = frozenset(
    {
        MediaRepresentationKind.EXTRACTED_TEXT,
        MediaRepresentationKind.OCR_TEXT,
        MediaRepresentationKind.IMAGE_CAPTION,
        MediaRepresentationKind.TRANSCRIPT_SEGMENT,
    }
)
_SEARCHABLE_MEDIA_STATUSES = frozenset(
    {MediaRepresentationStatus.CURRENT, MediaRepresentationStatus.PARTIAL}
)


@dataclass(frozen=True)
class MediaTextSearchResult:
    """A text-vector hit with immutable media evidence metadata."""

    representation_id: UUID
    resource_version_id: UUID
    kind: MediaRepresentationKind
    locators: tuple[dict[str, object], ...]
    coverage_fraction: float
    text: str
    score: float


@dataclass(frozen=True)
class VisualSearchResult:
    """A visual-space hit. Scores are meaningful only within ``space``."""

    representation_id: UUID
    resource_version_id: UUID
    space: str
    dimension: int
    locators: tuple[dict[str, object], ...]
    coverage_fraction: float
    score: float


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
        self._media_table_name = f"{table_name}_media_text"
        self._media_tbl = None
        self._visual_tables: dict[tuple[str, int], object] = {}

    def _visual_table_name(self, space: str, dimension: int) -> str:
        """Return a stable Lance table name isolated by space and dimension."""
        safe_space = re.sub(r"[^a-zA-Z0-9_]", "_", space)
        return f"{self._table_name}_visual_{safe_space}_{dimension}"

    def init_visual_table(self, space: str, dimension: int) -> None:
        """Create/open the index for exactly one compatible visual space."""
        if dimension < 1:
            raise ValueError("visual embedding dimension must be positive")
        key = (space, dimension)
        table_name = self._visual_table_name(*key)
        schema = pa.schema(
            [
                ("representation_id", pa.string()),
                ("resource_version_id", pa.string()),
                ("locators_json", pa.string()),
                ("coverage_fraction", pa.float32()),
                ("vector", pa.list_(pa.float32(), dimension)),
            ]
        )
        if table_name not in self._db.list_tables().tables:
            table = self._db.create_table(table_name, schema=schema, mode="overwrite")
        else:
            table = self._db.open_table(table_name)
        self._visual_tables[key] = table

    @staticmethod
    def _visual_payload(representation: DerivedRepresentation) -> tuple[str, list[float]]:
        if representation.kind is not MediaRepresentationKind.VISUAL_EMBEDDING:
            raise ValueError("only visual_embedding representations can enter a visual index")
        if representation.textual_payload is None:
            raise ValueError("visual embedding representation has no payload")
        try:
            payload = json.loads(representation.textual_payload)
            space = payload["space"]
            vector = payload["embedding"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid visual embedding payload") from exc
        if not isinstance(space, str) or not space or not isinstance(vector, list) or not vector:
            raise ValueError("invalid visual embedding payload")
        if not all(isinstance(value, (int, float)) for value in vector):
            raise ValueError("visual embedding must contain only numeric values")
        return space, [float(value) for value in vector]

    def upsert_visual_embeddings(self, representations: list[DerivedRepresentation]) -> None:
        """Project cached visual vectors into their compatible-space indexes.

        No table ever mixes a model space or vector dimension, preventing an
        accidental raw-score comparison across encoders.
        """
        for representation in representations:
            if representation.status not in _SEARCHABLE_MEDIA_STATUSES:
                continue
            space, vector = self._visual_payload(representation)
            key = (space, len(vector))
            if key not in self._visual_tables:
                self.init_visual_table(*key)
            table = self._visual_tables[key]
            table.delete(f"representation_id = '{representation.id}'")
            row = {
                "representation_id": str(representation.id),
                "resource_version_id": str(representation.resource_version_id),
                "locators_json": json.dumps(
                    [locator.model_dump(mode="json") for locator in representation.locators],
                    sort_keys=True,
                ),
                "coverage_fraction": representation.coverage.coverage_fraction,
                "vector": vector,
            }
            table.add(pa.Table.from_pylist([row], schema=table.schema))

    def search_visual(
        self, space: str, query_vector: list[float], k: int = 8
    ) -> list[VisualSearchResult]:
        """Search one exact visual space/dimension; incompatible input is rejected."""
        key = (space, len(query_vector))
        if key not in self._visual_tables:
            table_name = self._visual_table_name(*key)
            if table_name not in self._db.list_tables().tables:
                return []
            self._visual_tables[key] = self._db.open_table(table_name)
        rows = self._visual_tables[key].search(query_vector).limit(k).to_list()
        return [
            VisualSearchResult(
                representation_id=UUID(row["representation_id"]),
                resource_version_id=UUID(row["resource_version_id"]),
                space=space,
                dimension=len(query_vector),
                locators=tuple(json.loads(row["locators_json"])),
                coverage_fraction=float(row["coverage_fraction"]),
                score=1.0 / (1.0 + row["_distance"]),
            )
            for row in rows
        ]

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

    def init_media_text_table(self, embed_dim: int) -> None:
        """Initialize the separate, compatible text projection for media evidence.

        Media-derived text deliberately does not alter the legacy ``chunks``
        schema.  It carries the representation and locator fields needed to
        cite OCR, captions, and transcript segments precisely.
        """
        schema = pa.schema(
            [
                ("representation_id", pa.string()),
                ("resource_version_id", pa.string()),
                ("kind", pa.string()),
                ("status", pa.string()),
                ("text", pa.string()),
                ("locators_json", pa.string()),
                ("coverage_fraction", pa.float32()),
                ("vector", pa.list_(pa.float32(), embed_dim)),
            ]
        )
        if self._media_table_name not in self._db.list_tables().tables:
            self._media_tbl = self._db.create_table(
                self._media_table_name, schema=schema, mode="overwrite"
            )
        else:
            self._media_tbl = self._db.open_table(self._media_table_name)

    def upsert_media_text(
        self,
        representations: list[DerivedRepresentation],
        vectors: list[list[float]],
    ) -> None:
        """Project searchable media text without dropping locator provenance."""
        if len(representations) != len(vectors):
            raise ValueError("len(representations) != len(vectors)")
        if not representations:
            return
        if self._media_tbl is None:
            self.init_media_text_table(len(vectors[0]))

        rows: list[dict[str, object]] = []
        for representation, vector in zip(representations, vectors, strict=True):
            if representation.kind not in _TEXTUAL_MEDIA_KINDS:
                raise ValueError(f"{representation.kind} is not a textual media representation")
            if representation.status not in _SEARCHABLE_MEDIA_STATUSES:
                continue
            assert representation.textual_payload is not None
            self._media_tbl.delete(f"representation_id = '{representation.id}'")
            rows.append(
                {
                    "representation_id": str(representation.id),
                    "resource_version_id": str(representation.resource_version_id),
                    "kind": representation.kind.value,
                    "status": representation.status.value,
                    "text": representation.textual_payload,
                    "locators_json": json.dumps(
                        [locator.model_dump(mode="json") for locator in representation.locators],
                        sort_keys=True,
                    ),
                    "coverage_fraction": representation.coverage.coverage_fraction,
                    "vector": vector,
                }
            )
        if rows:
            self._media_tbl.add(pa.Table.from_pylist(rows, schema=self._media_tbl.schema))

    def search_media_text(
        self, query_vector: list[float], k: int = 8
    ) -> list[MediaTextSearchResult]:
        """Search media-derived text, returning its immutable citation metadata."""
        if self._media_tbl is None:
            if self._media_table_name not in self._db.list_tables().tables:
                return []
            self._media_tbl = self._db.open_table(self._media_table_name)
        rows = self._media_tbl.search(query_vector).limit(k).to_list()
        return [
            MediaTextSearchResult(
                representation_id=UUID(row["representation_id"]),
                resource_version_id=UUID(row["resource_version_id"]),
                kind=MediaRepresentationKind(row["kind"]),
                locators=tuple(json.loads(row["locators_json"])),
                coverage_fraction=float(row["coverage_fraction"]),
                text=row["text"],
                score=1.0 / (1.0 + row["_distance"]),
            )
            for row in rows
        ]

    def delete_media_by_resource_version(self, resource_version_id: UUID) -> None:
        """Remove stale media projections while keeping authority untouched."""
        if self._media_tbl is None and self._media_table_name in self._db.list_tables().tables:
            self._media_tbl = self._db.open_table(self._media_table_name)
        if self._media_tbl is not None:
            self._media_tbl.delete(f"resource_version_id = '{resource_version_id}'")
        for table in self._visual_tables.values():
            table.delete(f"resource_version_id = '{resource_version_id}'")
        for table_name in self._db.list_tables().tables:
            if not table_name.startswith(f"{self._table_name}_visual_"):
                continue
            table = self._db.open_table(table_name)
            table.delete(f"resource_version_id = '{resource_version_id}'")

    def rebuild_media_projections(self, representations: list[DerivedRepresentation]) -> None:
        """Rebuild media vectors from authoritative cached representations only."""
        for table_name in self._db.list_tables().tables:
            if table_name == self._media_table_name or table_name.startswith(
                f"{self._table_name}_visual_"
            ):
                self._db.drop_table(table_name)
        self._media_tbl = None
        self._visual_tables.clear()
        textual = [item for item in representations if item.kind in _TEXTUAL_MEDIA_KINDS]
        if textual:
            raise ValueError("text media rebuild requires cached text embedding vectors")
        self.upsert_visual_embeddings(representations)

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
        chunks: list[
            tuple[str, str, int, str, int]
        ],  # (chunk_id, file_id, ordinal, text, token_count)
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
                    rows.append(
                        {
                            "id": chunk_id,
                            "file_id": file_id,
                            "ordinal": ordinal,
                            "text": text,
                            "vector": vector_lookup[chunk_id],
                            "token_count": token_count,
                        }
                    )

            # Bulk insert all chunks
            if rows:
                tbl = pa.Table.from_pylist(rows, schema=schema)
                self._tbl.add(tbl)

    def close(self) -> None:
        """Best-effort cleanup (LanceDB has nothing to close; kept for symmetry)."""
        pass
