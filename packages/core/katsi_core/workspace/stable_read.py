"""Configured stable filesystem reads for reconciliation."""

from __future__ import annotations

from pathlib import Path
from time import sleep

from blake3 import blake3

from katsi_core.config import IngestSettings, ObserverSettings

_HASH_CHUNK_BYTES = 1_048_576


def is_supported_path(
    path: Path, root: Path, ingest: IngestSettings, observer: ObserverSettings
) -> bool:
    relative = path.relative_to(root).as_posix()
    if (
        path.name.startswith(observer.reserved_path_prefix)
        or path.stat().st_size > observer.max_file_bytes
    ):
        return False
    return any(
        path.match(pattern) or relative == pattern for pattern in ingest.include_globs
    ) and not any(path.match(pattern) for pattern in ingest.exclude_globs)


def stable_content_hash(
    path: Path, root: Path, ingest: IngestSettings, observer: ObserverSettings
) -> str | None:
    """Return a hash only when metadata is stable across a configured retry window."""
    if not path.is_file() or not is_supported_path(path, root, ingest, observer):
        return None
    for attempt in range(observer.stable_read_retries + 1):
        # The read itself is the stability window: a file still being written
        # changes mtime or size across it. Only a file that already looked
        # unstable pays the debounce, so a full scan of a large tree does not
        # sleep once per file.
        if attempt and observer.debounce_seconds:
            sleep(observer.debounce_seconds)
        before = path.stat()
        hasher = blake3()
        with path.open("rb") as handle:
            while chunk := handle.read(_HASH_CHUNK_BYTES):
                hasher.update(chunk)
        after = path.stat()
        if before.st_mtime_ns == after.st_mtime_ns and before.st_size == after.st_size:
            return hasher.hexdigest()
        if observer.stable_read_retry_seconds:
            sleep(observer.stable_read_retry_seconds)
    return None
