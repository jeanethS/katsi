#!/usr/bin/env python3
"""OCR wrapper: translate `tesseract` TSV output into the katsi OCR contract.

Contract (matches `build_ocr_pipeline_definition` in
`katsi_core.media.image_pipeline`):

    ocr_tesseract.py <input_path> <output_path> --lang <language>

Writes a JSON document to `<output_path>` with:

- a required `text` key (whole-image OCR text), and
- an optional `regions` key: list of {"text", "bbox", "confidence"} where
  `bbox` is normalized [x, y, w, h] against the image dimensions. `regions`
  is omitted entirely when no confidence data is available.

The payload is built in full before anything is written. On any failure the
wrapper exits non-zero without writing the output file, so a partial or
malformed document is never emitted. No network access; opens no sockets.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

TESSERACT = "tesseract"
MAGICK = "magick"
IDENTIFY = (MAGICK, "identify")

# tesseract TSV confidence sentinel for non-text rows.
_NO_CONFIDENCE = -1.0


class OcrWrapperError(Exception):
    """Any failure that must result in a non-zero exit and no output file."""


def _run(command: Sequence[str]) -> str:
    """Run a subprocess and return stdout; raise on non-zero exit."""
    try:
        result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            list(command),
            capture_output=True,
            check=False,
            timeout=120,
        )
    except OSError as exc:
        raise OcrWrapperError(f"failed to run {command[0]!r}: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise OcrWrapperError(f"{command[0]} exited with {result.returncode}: {stderr[:500]}")
    return result.stdout.decode("utf-8", errors="replace")


def _image_dimensions(input_path: str) -> tuple[float, float] | None:
    """Return (width, height) via ImageMagick identify, or None if unavailable."""
    try:
        out = _run([*IDENTIFY, "-ping", "-format", "%w %h", input_path])
    except OcrWrapperError:
        return None
    parts = out.split()
    if len(parts) != 2:
        return None
    try:
        width, height = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _ocr_tsv(input_path: str, language: str, workdir: str) -> tuple[str, str]:
    """Return (TSV, path tesseract read), transcoding with magick if needed.

    tesseract has no HEIF decoder, so an iPhone HEIC fails outright. Only such
    a refusal triggers a transcode -- the common case pays no extra process --
    and a failed transcode surfaces tesseract's own error, not magick's.
    """
    # tesseract's own option is `-l`; `--lang` is this wrapper's interface so
    # the owner-facing argument template never depends on tool spelling.
    command = [TESSERACT, input_path, "stdout", "-l", language, "tsv"]
    try:
        return _run(command), input_path
    except OcrWrapperError as undecodable:
        converted = str(Path(workdir) / "input.png")
        try:
            _run([MAGICK, input_path, "-auto-orient", converted])
        except OcrWrapperError:
            raise undecodable from None
        command[1] = converted
        return _run(command), converted


def parse_tsv(tsv_text: str) -> tuple[str, list[tuple[str, int, int, int, int, float]]]:
    """Parse tesseract TSV into (whole text, raw word boxes).

    Words are grouped into lines by (block, paragraph, line); each line's box
    is the union of its word boxes. Rows with the no-confidence sentinel are
    excluded from regions but still contribute to the text.
    """
    lines = tsv_text.splitlines()
    if len(lines) < 2:
        return "", []

    headers = lines[0].lower().split("\t")
    required = (
        "level",
        "block_num",
        "par_num",
        "line_num",
        "left",
        "top",
        "width",
        "height",
        "conf",
        "text",
    )
    if any(col not in headers for col in required):
        raise OcrWrapperError(f"unrecognized tesseract TSV header: {lines[0]!r}")
    idx = {col: headers.index(col) for col in required}

    line_words: dict[tuple[int, int, int], list[tuple[str, int, int, int, int, float]]] = {}
    order: list[tuple[int, int, int]] = []
    text_lines: list[str] = []

    for row in lines[1:]:
        cells = row.split("\t")
        if len(cells) != len(headers):
            continue
        if cells[idx["level"]].strip() != "5":  # level 5 = word
            continue
        word = cells[idx["text"]]
        conf_raw = cells[idx["conf"]].strip()
        try:
            left = int(cells[idx["left"]])
            top = int(cells[idx["top"]])
            width = int(cells[idx["width"]])
            height = int(cells[idx["height"]])
        except ValueError as exc:
            raise OcrWrapperError(f"malformed TSV geometry row: {row!r}") from exc

        if not word.strip():
            continue
        key = (
            int(cells[idx["block_num"]]),
            int(cells[idx["par_num"]]),
            int(cells[idx["line_num"]]),
        )
        bucket = line_words.setdefault(key, [])
        if not bucket:
            order.append(key)
            text_lines.append([])
        text_lines[-1].append(word)
        try:
            conf = float(conf_raw)
        except ValueError as exc:
            raise OcrWrapperError(f"malformed TSV confidence value: {conf_raw!r}") from exc
        if conf == _NO_CONFIDENCE:
            continue
        bucket.append((word, left, top, width, height, conf))

    text = "\n".join(" ".join(words) for words in text_lines)

    regions: list[tuple[str, int, int, int, int, float]] = []
    for key in order:
        bucket = line_words.get(key, [])
        if not bucket:
            continue
        lefts = [w[1] for w in bucket]
        tops = [w[2] for w in bucket]
        rights = [w[1] + w[3] for w in bucket]
        bottoms = [w[2] + w[4] for w in bucket]
        confidences = [w[5] for w in bucket]
        mean_conf = sum(confidences) / len(confidences) / 100.0
        regions.append(
            (
                " ".join(w[0] for w in bucket),
                min(lefts),
                min(tops),
                max(rights) - min(lefts),
                max(bottoms) - min(tops),
                mean_conf,
            )
        )
    return text, regions


def build_payload(
    text: str,
    regions: Sequence[tuple[str, int, int, int, int, float]],
    dimensions: tuple[float, float] | None,
) -> dict[str, object]:
    """Assemble the full output document; regions normalized only when dims exist."""
    payload: dict[str, object] = {"text": text}
    if dimensions is None:
        return payload
    img_w, img_h = dimensions
    normalized: list[dict[str, object]] = []
    for region_text, left, top, width, height, conf in regions:
        normalized.append(
            {
                "text": region_text,
                "bbox": [
                    round(left / img_w, 6),
                    round(top / img_h, 6),
                    round(width / img_w, 6),
                    round(height / img_h, 6),
                ],
                "confidence": round(conf, 4),
            }
        )
    if normalized:
        payload["regions"] = normalized
    return payload


def main(argv: Sequence[str]) -> int:
    args = list(argv)
    language: str | None = None
    if "--lang" in args:
        i = args.index("--lang")
        if i + 1 >= len(args):
            print("ocr_tesseract: --lang requires a value", file=sys.stderr)
            return 2
        language = args[i + 1]
        del args[i : i + 2]
    if len(args) != 2:
        print(
            "usage: ocr_tesseract.py <input_path> <output_path> --lang <language>",
            file=sys.stderr,
        )
        return 2
    input_path, output_path = args
    if language is None:
        # Language must be explicit so it lands in fixed_args and therefore in
        # the pipeline fingerprint; a wrapper-side default would change OCR
        # results without invalidating cached representations.
        print("ocr_tesseract: an explicit --lang is required", file=sys.stderr)
        return 2

    try:
        with tempfile.TemporaryDirectory(prefix="katsi-ocr-") as workdir:
            tsv, read_path = _ocr_tsv(input_path, language, workdir)
            text, regions = parse_tsv(tsv)
            # Boxes are normalized against the image tesseract actually read,
            # which for a transcoded file is the auto-oriented one -- the same
            # orientation the thumbnail pipeline produces.
            payload = build_payload(text, regions, _image_dimensions(read_path))
    except OcrWrapperError as exc:
        print(f"ocr_tesseract: {exc}", file=sys.stderr)
        return 1

    document = json.dumps(payload, ensure_ascii=False)
    try:
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(document)
    except OSError as exc:
        print(f"ocr_tesseract: cannot write output: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
