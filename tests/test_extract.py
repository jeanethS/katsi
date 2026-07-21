"""Tests for katsi_core.ingest.extract."""

from pathlib import Path

import pytest

from katsi_core.ingest.extract import _get_markitdown, extract_text


def test_extract_markdown_file(tmp_path: Path) -> None:
    """Write a small .md file; extract_text returns non-empty markdown."""
    p = tmp_path / "hello.md"
    p.write_text("# Hello\n\nThis is a test.")
    result = extract_text(p)
    assert "# Hello" in result
    assert "This is a test." in result


def test_extract_python_file(tmp_path: Path) -> None:
    """Write a .py file with a function; extract_text returns text containing the function name."""
    p = tmp_path / "greet.py"
    p.write_text("def greet(name: str) -> str:\n    return f'hello {name}'\n")
    result = extract_text(p)
    assert "greet" in result


def test_extract_missing_file_returns_empty(tmp_path: Path) -> None:
    """Path that does not exist returns ''."""
    result = extract_text(tmp_path / "nonexistent.md")
    assert result == ""


def test_extract_empty_file_returns_empty(tmp_path: Path) -> None:
    """A 0-byte file returns ''."""
    p = tmp_path / "empty.md"
    p.write_text("")
    result = extract_text(p)
    assert result == ""


def test_extract_failure_returns_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When markitdown.convert raises, extract_text returns '' and does not propagate."""
    p = tmp_path / "bad.md"
    p.write_text("some content")

    def _broken_convert(*args: object, **kwargs: object) -> object:
        msg = "simulated failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(_get_markitdown(), "convert", _broken_convert)
    result = extract_text(p)
    assert result == ""
