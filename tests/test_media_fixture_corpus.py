"""Inventory checks for the synthetic, MIT-licensed media fixture corpus."""

from __future__ import annotations

import json
from pathlib import Path


def test_media_fixture_corpus_covers_required_dogfood_categories() -> None:
    corpus_path = Path(__file__).parent / "fixtures" / "media" / "corpus.json"
    corpus = json.loads(corpus_path.read_text())

    assert {item["id"] for item in corpus} == {
        "screenshot",
        "diagram",
        "scan",
        "speech",
        "multi-speaker-audio",
        "slides",
        "silent-video",
        "speech-plus-visual-video",
    }
    assert {item["license"] for item in corpus} == {"MIT"}
    assert all((Path.cwd() / item["builder"]).is_file() for item in corpus)
