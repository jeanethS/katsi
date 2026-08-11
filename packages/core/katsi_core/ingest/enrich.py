from __future__ import annotations

import logging

from katsi_core.models import Chunk, FileRecord, IndexStatus
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore

logger = logging.getLogger(__name__)

# A resource publishes current chunks only while its content was successfully
# processed. Deleted, errored, or not-yet-indexed resources are excluded so
# stale vectors never pollute current retrieval.
_PUBLISHABLE_STATUSES: frozenset[IndexStatus] = frozenset({IndexStatus.INDEXED, IndexStatus.STALE})


def apply_extraction(
    file_record: FileRecord,
    extraction,
    graph: GraphStore,
) -> None:
    """Push the Extraction result into the graph.

    Order matters for idempotency:
      1. remove the resource's previous current projection
      2. upsert_file(file_record)
      3. add_mentions(file_id, entities)
      4. add_about(file_id, topics)
      5. persist reference intent and backfill current edges.

    The graph is a current projection, not historical provenance. Removing the
    old File node prevents changed extraction from retaining stale edges.
    """
    graph.delete_by_file(file_record.id)
    graph.upsert_file(file_record)
    if extraction.entities:
        graph.add_mentions(file_record.id, extraction.entities)
    if extraction.topics:
        graph.add_about(file_record.id, extraction.topics)
    graph.replace_reference_intents(file_record.id, extraction.references)
    graph.backfill_references()


def project_chunks(
    file_record: FileRecord,
    chunks: list[Chunk],
    vectors: list[list[float]],
    vector_store: VectorStore,
) -> None:
    """Push chunks into the vector projection.

    Mirrors ``apply_extraction``'s replace semantics for the graph projection:
      1. remove the resource's previous current chunks
      2. publish the new chunks only when the resource is current.

    Deleted or errored resources are excluded: their previous chunks are
    removed and no new chunks are written, so stale vectors never survive a
    failed re-index or a resource leaving current state.
    """
    vector_store.delete_by_file(file_record.id)
    if file_record.status not in _PUBLISHABLE_STATUSES:
        return
    if chunks:
        vector_store.upsert_chunks(chunks, vectors)
