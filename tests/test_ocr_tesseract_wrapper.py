"""Unit tests for tools/media/ocr_tesseract.py over captured tesseract output.

The wrapper is exercised both at function level (parsing real TSV captured
from tesseract 5) and end to end with fake `tesseract` / `magick` binaries
injected through PATH, so CI never requires either tool.
"""

from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path
from typing import Any

import pytest

_WRAPPER_PATH = Path(__file__).resolve().parent.parent / "tools" / "media" / "ocr_tesseract.py"

_spec = importlib.util.spec_from_file_location("ocr_tesseract_wrapper", _WRAPPER_PATH)
assert _spec is not None and _spec.loader is not None
wrapper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wrapper)


# Captured from `tesseract <image> stdout -l eng tsv` against an image reading
# "HOLA MUNDO 123" rendered at ~222 dpi on a 400x100 canvas.
TSV_WITH_TEXT = """level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext
1\t1\t0\t0\t0\t0\t0\t0\t400\t100\t-1\t
5\t1\t1\t1\t1\t1\t22\t33\t80\t17\t96.0\tHOLA
5\t1\t1\t1\t1\t2\t110\t33\t60\t17\t97.5\tMUNDO
5\t1\t1\t1\t1\t3\t178\t33\t27\t17\t95.0\t123
"""

# Captured shape for a word row carrying the no-confidence sentinel (-1):
# contributes to text but must not produce a region box.
TSV_NO_CONFIDENCE = """level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext
1\t1\t0\t0\t0\t0\t0\t0\t400\t100\t-1\t
5\t1\t1\t1\t1\t1\t22\t33\t80\t17\t-1\tHOLA
"""

TSV_EMPTY = """level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext
1\t1\t0\t0\t0\t0\t0\t0\t400\t100\t-1\t
"""


class FakeTools:
    """Install fake tesseract/magick executables on PATH for one test."""

    def __init__(self, tmp_path: Path, tsv: str | None, *, identify: str = "400 100") -> None:
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir(exist_ok=True)
        self._script("magick", f'#!/bin/sh\nif [ "$1" = "identify" ]; then echo "{identify}"; fi\n')
        if tsv is None:
            self._script("tesseract", "#!/bin/sh\necho 'boom' >&2\nexit 1\n")
        else:
            script = (
                "#!/bin/sh\n"
                'for last in "$@"; do :; done\n'
                'if [ "$last" != "tsv" ]; then echo \'expecting tsv config\' >&2; exit 2; fi\n'
                f"cat <<'EOF'\n{tsv}\nEOF\n"
            )
            self._script("tesseract", script)

    def _script(self, name: str, body: str) -> None:
        path = self.bin_dir / name
        path.write_text(body)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


@pytest.fixture()
def run_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    def _run(
        tsv: str | None,
        *,
        identify: str = "400 100",
        extra_args: list[str] | None = None,
        input_image: Path | None = None,
    ) -> tuple[int, Path]:
        FakeTools(tmp_path, tsv, identify=identify)
        monkeypatch.setenv("PATH", str(tmp_path / "bin"), prepend=":")
        out = tmp_path / "out.json"
        args = [str(input_image or tmp_path / "in.png"), str(out), "--lang", "eng"]
        if extra_args is not None:
            args = [str(input_image or tmp_path / "in.png"), str(out), *extra_args]
        code = wrapper.main(args)
        return code, out

    return _run


def test_text_with_regions_normalizes_boxes(run_wrapper: Any, tmp_path: Path) -> None:
    code, out = run_wrapper(TSV_WITH_TEXT)
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["text"] == "HOLA MUNDO 123"
    assert payload["regions"] == [
        {
            "text": "HOLA MUNDO 123",
            # Union of the three word boxes, normalized by 400x100.
            "bbox": [0.055, 0.33, 0.4575, 0.17],
            "confidence": pytest.approx((96.0 + 97.5 + 95.0) / 3 / 100, abs=1e-4),
        }
    ]


def test_text_only_when_confidence_unavailable_omits_regions(
    run_wrapper: Any,
) -> None:
    code, out = run_wrapper(TSV_NO_CONFIDENCE)
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload == {"text": "HOLA"}


def test_no_detectable_text_is_valid_not_a_failure(run_wrapper: Any) -> None:
    code, out = run_wrapper(TSV_EMPTY)
    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {"text": ""}


def test_nonzero_tesseract_exit_writes_no_output(run_wrapper: Any) -> None:
    code, out = run_wrapper(None)
    assert code != 0
    assert not out.exists()


def test_dimensions_failure_still_emits_text_only(run_wrapper: Any) -> None:
    code, out = run_wrapper(TSV_WITH_TEXT, identify="")
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload == {"text": "HOLA MUNDO 123"}


def test_undecodable_input_is_transcoded_then_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A HEIC tesseract cannot open is OCR'd through a magick PNG transcode."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    # Refuses anything but the transcoded PNG, the way tesseract refuses HEIF.
    (bin_dir / "tesseract").write_text(
        "#!/bin/sh\n"
        f'echo "tesseract $1" >> {calls}\n'
        'case "$1" in\n'
        "  *input.png)\n"
        f"    cat <<'EOF'\n{TSV_WITH_TEXT}\nEOF\n"
        "    ;;\n"
        "  *) echo 'Error during processing.' >&2; exit 1 ;;\n"
        "esac\n"
    )
    (bin_dir / "magick").write_text(
        "#!/bin/sh\n"
        f'echo "magick $*" >> {calls}\n'
        'if [ "$1" = "identify" ]; then echo "400 100"; exit 0; fi\n'
        'cp "$1" "$3"\n'
    )
    for name in ("tesseract", "magick"):
        (bin_dir / name).chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir), prepend=":")
    source = tmp_path / "photo.heic"
    source.write_bytes(b"heic-bytes")
    out = tmp_path / "out.json"

    code = wrapper.main([str(source), str(out), "--lang", "spa+eng"])

    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["text"] == "HOLA MUNDO 123"
    log = calls.read_text()
    assert "-auto-orient" in log
    assert log.count("tesseract ") == 2  # original refused, transcode read


def test_transcode_failure_reports_the_tesseract_error(run_wrapper: Any, tmp_path: Path) -> None:
    """When magick cannot convert either, no output file and a non-zero exit."""
    code, out = run_wrapper(None, input_image=tmp_path / "broken.heic")

    assert code != 0
    assert not out.exists()


def test_explicit_language_is_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTools(tmp_path, TSV_WITH_TEXT)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"), prepend=":")
    out = tmp_path / "out.json"
    code = wrapper.main([str(tmp_path / "in.png"), str(out)])
    assert code == 2
    assert not out.exists()


def test_parse_tsv_rejects_unknown_header() -> None:
    with pytest.raises(wrapper.OcrWrapperError):
        wrapper.parse_tsv("col_a\tcol_b\n1\t2\n")


def test_parse_tsv_rejects_malformed_geometry() -> None:
    bad = (
        "level\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\tx\ty\tw\th\t90.0\tword\n"
    )
    with pytest.raises(wrapper.OcrWrapperError):
        wrapper.parse_tsv(bad)
