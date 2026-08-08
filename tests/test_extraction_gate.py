import pytest
from pydantic import ValidationError

from katsi_core.workspace.extraction_gate import ExtractionGate


def test_extraction_gate_retries_once_and_rejects_invalid_extra_fields() -> None:
    attempts = 0

    def produce() -> object:
        nonlocal attempts
        attempts += 1
        return {"summary": "x", "entities": [], "topics": [], "references": [], "unsafe": True}

    with pytest.raises(ValidationError):
        ExtractionGate().validate(produce)
    assert attempts == 2


def test_extraction_gate_returns_valid_contract() -> None:
    extraction = ExtractionGate().validate(
        lambda: {"summary": "x", "entities": [], "topics": [], "references": []}
    )
    assert extraction.summary == "x"
