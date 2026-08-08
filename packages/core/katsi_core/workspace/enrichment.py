"""Content-addressed enrichment compatibility contracts."""

from __future__ import annotations

import json
from hashlib import sha256

from pydantic import Field

from katsi_core.workspace.contracts import ImmutableModel


class EnrichmentFingerprint(ImmutableModel):
    content_hash: str = Field(min_length=16)
    extraction_contract_version: str = Field(min_length=1)
    model_identity: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    chunking_version: str = Field(min_length=1)
    semantic_settings_version: str = Field(min_length=1)

    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        return sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
