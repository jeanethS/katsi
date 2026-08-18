"""Cross-modal Claim dogfood fixture for multimedia-understanding task 14.4."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from katsi_core.config import SQLiteSettings
from katsi_core.media.contracts import (
    ImageRegionLocator,
    PageLocator,
    TimeRangeLocator,
    VideoFrameLocator,
)
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.authorization import AuthorizationService
from katsi_core.workspace.claims import ClaimService
from katsi_core.workspace.contracts import (
    CapabilityGrant,
    CapabilityOperationClass,
    Claim,
    ClaimEvidence,
    ClaimEvidenceKind,
    RiskClass,
)
from katsi_core.workspace.identity import IdentityService


def _grant_claim_capability(
    database: WorkspaceSQLite, identity_id: UUID, workspace_id: UUID
) -> None:
    grant = CapabilityGrant(
        id=uuid4(),
        identity_id=identity_id,
        workspace_id=workspace_id,
        operation_classes=frozenset({CapabilityOperationClass.CLAIM}),
        resource_scope=(),
        maximum_risk=RiskClass.LOW,
        issued_at=datetime.now(UTC),
        expires_at=None,
        revoked_at=None,
    )
    with database.connection() as connection:
        connection.execute(
            "INSERT INTO capability_grants VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(grant.id),
                str(grant.identity_id),
                str(grant.workspace_id),
                '["claim"]',
                "[]",
                "low",
                grant.issued_at.isoformat(),
                None,
                None,
            ),
        )


def _media_evidence(
    claim_id: UUID, resource_version_id: UUID, locator: object, label: str
) -> ClaimEvidence:
    locator_json = json.dumps(locator.model_dump(mode="json"), sort_keys=True)  # type: ignore[attr-defined]
    return ClaimEvidence(
        id=uuid4(),
        claim_id=claim_id,
        kind=ClaimEvidenceKind.RESOURCE_VERSION,
        reference={
            "resource_id": str(uuid4()),
            "resource_version_id": str(resource_version_id),
            "representation_id": str(locator.representation_id),  # type: ignore[attr-defined]
            "locator": locator_json,
            "label": label,
        },
        created_at=datetime.now(UTC),
    )


def test_cross_modal_claim_retains_each_citable_media_locator(tmp_path: Path) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, target_version=3)
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Dogfood")
    identities = IdentityService(database)
    author = identities.register("Dogfood agent", "test")
    _grant_claim_capability(database, author.id, workspace.id)
    service = ClaimService(database, identities, AuthorizationService(database))

    claim = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        text="The demo explains the architecture shown in the screenshot and scan.",
        confidence=0.8,
        created_at=datetime.now(UTC),
    )
    image_version, pdf_version, audio_version, video_version = (uuid4() for _ in range(4))
    image_representation, pdf_representation, audio_representation, video_representation = (
        uuid4() for _ in range(4)
    )
    evidence = (
        _media_evidence(
            claim.id,
            image_version,
            ImageRegionLocator(
                resource_version_id=image_version,
                representation_id=image_representation,
                bounding_box=(0.1, 0.2, 0.5, 0.3),
            ),
            "image region",
        ),
        _media_evidence(
            claim.id,
            pdf_version,
            PageLocator(
                resource_version_id=pdf_version,
                representation_id=pdf_representation,
                page_number=2,
                bounding_box=(0.2, 0.2, 0.4, 0.2),
            ),
            "PDF page",
        ),
        _media_evidence(
            claim.id,
            audio_version,
            TimeRangeLocator(
                resource_version_id=audio_version,
                representation_id=audio_representation,
                start_ms=1_000,
                end_ms=3_500,
            ),
            "audio interval",
        ),
        _media_evidence(
            claim.id,
            video_version,
            VideoFrameLocator(
                resource_version_id=video_version,
                representation_id=video_representation,
                timestamp_ms=4_200,
                frame_index=126,
            ),
            "video keyframe",
        ),
    )

    service.publish(claim, evidence)

    with database.connection() as connection:
        rows = connection.execute(
            "SELECT reference_json FROM claim_evidence WHERE claim_id = ? ORDER BY id",
            (str(claim.id),),
        ).fetchall()
    locators = [json.loads(json.loads(row["reference_json"])["locator"]) for row in rows]
    assert {locator["locator_type"] for locator in locators} == {
        "image_region",
        "page",
        "time_range",
        "video_frame",
    }
    assert {locator["resource_version_id"] for locator in locators} == {
        str(image_version),
        str(pdf_version),
        str(audio_version),
        str(video_version),
    }
