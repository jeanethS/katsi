from __future__ import annotations

import logging
import os

from mnemo_core.models import FileRecord
from mnemo_core.store.graph import GraphStore

logger = logging.getLogger(__name__)


def _basename(ref: str) -> str:
    """Strip whitespace and path separators, return basename."""
    return os.path.basename(ref.strip().rstrip("/\\").strip())


def _resolve_reference(graph: GraphStore, ref: str) -> str | None:
    """Try to find a File node whose name matches the reference basename."""
    base = _basename(ref)
    if not base:
        return None
    try:
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


def apply_extraction(
    file_record: FileRecord,
    extraction,
    graph: GraphStore,
) -> None:
    """Push the Extraction result into the graph.

    Order matters for idempotency:
      1. upsert_file(file_record)
      2. add_mentions(file_id, entities)
      3. add_about(file_id, topics)
      4. add_reference edges for resolvable references.
    """
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
                    logger.debug(
                        "add_reference %s->%s failed: %r",
                        file_record.id,
                        target_id,
                        e,
                    )
