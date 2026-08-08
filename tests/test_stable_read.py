from pathlib import Path

from blake3 import blake3

from katsi_core.config import IngestSettings, ObserverSettings
from katsi_core.workspace.stable_read import stable_content_hash


def test_stable_hash_obeys_include_size_and_reserved_path_policies(tmp_path: Path) -> None:
    accepted = tmp_path / "note.md"
    accepted.write_text("hello", encoding="utf-8")
    reserved = tmp_path / ".katsi-stage-file.md"
    reserved.write_text("ignored", encoding="utf-8")
    oversized = tmp_path / "large.md"
    oversized.write_text("12345", encoding="utf-8")
    ingest = IngestSettings(include_globs=["**/*.md"], exclude_globs=[])
    observer = ObserverSettings(max_file_bytes=4)

    assert (
        stable_content_hash(accepted, tmp_path, ingest, ObserverSettings())
        == blake3(b"hello").hexdigest()
    )
    assert stable_content_hash(reserved, tmp_path, ingest, ObserverSettings()) is None
    assert stable_content_hash(oversized, tmp_path, ingest, observer) is None
