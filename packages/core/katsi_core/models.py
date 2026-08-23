"""katsi data models.

Strictly follows §5.1 of the architecture spec. Do not rename fields, do not
add defaults beyond what is specified.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class IndexStatus(StrEnum):
    PENDING = "pending"
    INDEXED = "indexed"
    STALE = "stale"
    ERROR = "error"
    DELETED = "deleted"
    SKIPPED = "skipped"  # tracked resource with no text to index (media families)


class FileRecord(BaseModel):
    id: str  # blake3(realpath), stable across content changes
    path: str  # absolute realpath
    name: str
    ext: str
    mime: str
    size_bytes: int
    mtime: float
    content_hash: str  # blake3 of file bytes — drives skip/reindex
    status: IndexStatus = IndexStatus.PENDING
    summary: str | None = None
    last_indexed_at: datetime | None = None
    error: str | None = None


class Chunk(BaseModel):
    id: str  # f"{file_id}:{ordinal}"
    file_id: str
    ordinal: int
    text: str
    token_count: int


class Extraction(BaseModel):
    """Strict JSON contract the local model must return."""

    summary: str
    entities: list[dict]  # {"name": str, "kind": "person|org|project"}
    topics: list[str]
    references: list[str]  # paths/filenames this file points at, if any


class EvidenceKind(StrEnum):
    VECTOR = "vector"
    ENTITY = "entity"
    TOPIC = "topic"
    REFERENCE_OUT = "reference_out"
    REFERENCE_IN = "reference_in"
    DUPLICATE = "duplicate"
    HOP_DECAY = "hop_decay"


class Evidence(BaseModel):
    """One line item on a FileHit's score. Contributions sum to the score
    (pre-clamp). See katsi-scoring-spec.md §3.2.
    """

    kind: EvidenceKind
    contribution: float  # signed points added to the score
    detail: str  # human-readable, e.g. "shares 3 entities: Kùzu, Leiden, bge-m3"


class FileHit(BaseModel):
    file_id: str
    path: str
    summary: str
    score: float
    why: str  # short relevance/relationship explanation
    evidence: list[Evidence] = []  # per-signal receipt; derives `why`


class ContextBundle(BaseModel):
    query: str
    files: list[FileHit]
    chunks: list[Chunk]  # only the few highest-scoring raw chunks
    relationships: list[str]  # human-readable graph sketch lines
    token_estimate: int
    projection_diagnostics: list[dict[str, object]] = Field(default_factory=list)
    projection_lag: bool = False
