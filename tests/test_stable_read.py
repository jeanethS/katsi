from pathlib import Path
from types import SimpleNamespace

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


def test_stable_hash_does_not_debounce_a_stable_file(tmp_path: Path, monkeypatch) -> None:
    """A full scan must not pay one debounce sleep per already-stable file."""
    slept: list[float] = []
    monkeypatch.setattr("katsi_core.workspace.stable_read.sleep", slept.append)
    accepted = tmp_path / "note.md"
    accepted.write_text("hello", encoding="utf-8")

    assert (
        stable_content_hash(accepted, tmp_path, IngestSettings(), ObserverSettings())
        == blake3(b"hello").hexdigest()
    )
    assert slept == []


def test_stable_hash_retries_with_debounce_while_a_file_keeps_changing(
    tmp_path: Path, monkeypatch
) -> None:
    """An unstable file still gets the configured retry window before giving up."""
    slept: list[float] = []
    monkeypatch.setattr("katsi_core.workspace.stable_read.sleep", slept.append)
    changing = tmp_path / "note.md"
    changing.write_text("hello", encoding="utf-8")
    real_stat = Path.stat
    counter = iter(range(1_000))
    monkeypatch.setattr(
        Path,
        "stat",
        lambda self, **kw: SimpleNamespace(
            st_mtime_ns=next(counter),
            st_size=(actual := real_stat(self, **kw)).st_size,
            st_mode=actual.st_mode,
        ),
    )

    observer = ObserverSettings(stable_read_retries=2)
    assert stable_content_hash(changing, tmp_path, IngestSettings(), observer) is None
    assert slept.count(observer.debounce_seconds) == 2


def test_stable_hash_streams_file_content(tmp_path: Path, monkeypatch) -> None:
    accepted = tmp_path / "note.md"
    accepted.write_bytes(b"a" * 2_000_000)
    monkeypatch.setattr(Path, "read_bytes", lambda _: (_ for _ in ()).throw(AssertionError()))

    assert (
        stable_content_hash(accepted, tmp_path, IngestSettings(), ObserverSettings())
        == blake3(b"a" * 2_000_000).hexdigest()
    )
