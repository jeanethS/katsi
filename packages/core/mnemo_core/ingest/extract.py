from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Module-level singleton — MarkItDown is expensive to construct.
_MARKITDOWN: MarkItDown | None = None  # noqa: F821


def _get_markitdown() -> MarkItDown:  # noqa: F821
    """Lazy singleton constructor. Imports markitdown only on first call."""
    global _MARKITDOWN
    if _MARKITDOWN is None:
        from markitdown import MarkItDown

        _MARKITDOWN = MarkItDown()
    return _MARKITDOWN


def extract_text(path: Path) -> str:
    """Extract markdown text from a file using markitdown.

    Accepts str or Path. Returns "" on:
    - file does not exist
    - file is empty
    - file extension is unsupported/filtered
    - markitdown raised an exception

    Always logs at INFO when starting and at WARN when extraction fails.
    Never raises out of this function — callers must get "" on failure.
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        logger.warning("extract_text: path missing or not a file: %s", p)
        return ""
    if p.stat().st_size == 0:
        logger.info("extract_text: empty file: %s", p)
        return ""
    try:
        md = _get_markitdown()
        result = md.convert(str(p))
        text = (result.text_content or "").strip()
        if not text:
            logger.info("extract_text: no text content from: %s", p)
        return text
    except Exception:  # noqa: BLE001
        logger.warning("extract_text: failed on %s", p, exc_info=True)
        return ""
