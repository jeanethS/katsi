"""Strict local extraction validation with exactly one retry."""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, ValidationError


class StrictExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str
    entities: list[dict[str, str]]
    topics: list[str]
    references: list[str]


class ExtractionGate:
    def validate(self, produce: Callable[[], object]) -> StrictExtraction:
        """Validate one result, retry once, then raise a terminal validation error."""
        last_error: ValidationError | None = None
        for _attempt in range(2):
            try:
                return StrictExtraction.model_validate(produce())
            except ValidationError as error:
                last_error = error
        assert last_error is not None
        raise last_error
