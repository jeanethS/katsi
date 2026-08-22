"""Media sampling settings shared by configuration and media contracts."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class ChunkingThresholds(BaseModel):
    """Configurable chunking strategy parameters for multimedia processing."""

    target_tokens: int = Field(default=512, ge=64, le=100000)
    overlap: int = Field(default=64, ge=0, le=10000)
    separator_hierarchy: list[str] = Field(default=["\n\n", "\n", ". ", " ", ""])

    @field_validator("overlap")
    @classmethod
    def overlap_less_than_target(cls, value: int, info) -> int:
        target = info.data.get("target_tokens", 512)
        if value >= target:
            raise ValueError(f"overlap ({value}) must be less than target_tokens ({target})")
        return value


class MediaSamplingSettings(BaseModel):
    """Sampling policy that contributes to media pipeline fingerprints."""

    chunking: ChunkingThresholds = Field(default_factory=ChunkingThresholds)

    @field_validator("chunking")
    @classmethod
    def validate_chunking_policy(cls, value: ChunkingThresholds) -> ChunkingThresholds:
        if value.overlap > value.target_tokens * 0.5:
            raise ValueError("overlap should not exceed 50% of target_tokens")
        return value

    def get_fingerprint_components(self) -> dict[str, str | int | tuple[str, ...]]:
        return {
            "chunking_target_tokens": self.chunking.target_tokens,
            "chunking_overlap": self.chunking.overlap,
            "chunking_separators": tuple(self.chunking.separator_hierarchy),
        }
