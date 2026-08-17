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

    def model_post_init(self, __context: object) -> None:
        """Validate that entity dictionaries have required fields."""
        super().model_post_init(__context)
        for entity in self.entities:
            if not isinstance(entity, dict):
                raise ValueError("Entity must be a dictionary")
            if "name" not in entity or "kind" not in entity:
                raise ValueError("Each entity must have 'name' and 'kind' fields")


class ExtractionGate:
    def validate(self, produce: Callable[[], object]) -> StrictExtraction:
        """Validate one result, retry once, then raise a terminal validation error.

        The gate attempts exactly 2 calls total: 1 initial attempt + 1 retry if needed.
        This handles both model failures (any exception) and validation failures (ValidationError).
        """
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                return StrictExtraction.model_validate(produce())
            except ValidationError as error:
                last_error = error
            except Exception as error:
                # Catch any exception from the produce() function (e.g., ValueError, RuntimeError)
                last_error = error
        assert last_error is not None
        raise last_error
