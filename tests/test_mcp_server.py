"""Smoke tests for the katsi FastMCP server tools."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from katsi_core.config import Settings, SQLiteSettings
from katsi_core.ingest.pipeline import IngestPipeline
from katsi_core.ingest.records import FileRecordStore
from katsi_core.media.contracts import (
    DerivedRepresentation,
    MediaCoverage,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineFingerprint,
    PipelineStage,
    ProducerProvenance,
    ResourceVersionId,
    TimeRangeLocator,
    WholeResourceLocator,
)
from katsi_core.media.registry import RepresentationRegistry
from katsi_core.models import Extraction
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.synth import SynthConfigError
from katsi_core.workspace.contracts import WorkspaceEventKind


class _FakeEmbed:
    def __init__(self, dim: int = 8):
        self.dim = dim
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [[0.5] * self.dim for _ in texts]


class _FakeLLM:
    def __init__(self, json_str: str):
        self.json_str = json_str
        self.calls = 0

    def extract(self, text, *, attempts: int = 2):
        self.calls += 1
        return Extraction(**json.loads(self.json_str))

    def chat(self, prompt, *, temperature: float = 0.2, model=None, max_tokens=None):
        return f"answer for prompt len={len(prompt)}"

    def _chat(self, system_prompt, user_text):
        return ""

    def chat_with_settings(self, *args, **kwargs):
        return ""


EXTRACTION_JSON = '{"summary":"doc summary","entities":[{"name":"Acme","kind":"org"}],"topics":["ai"],"references":[]}'


@pytest.fixture
def server_state(tmp_path):
    """Replace the mcp server `_state` with a fresh fixture-backed pipeline."""
    # Build local stores pointing at tmp_path
    s = Settings()
    # Override data_dir so writes go to tmp, not ~/.katsi.
    s.store.data_dir = tmp_path / "katsi_data"
    vectors = VectorStore(tmp_path / "katsi_data" / "vectors")
    vectors.init_table(8)
    graph = GraphStore(tmp_path / "katsi_data" / "graph")
    records = FileRecordStore(tmp_path / "katsi_data" / "records")
    embed = _FakeEmbed(dim=8)
    llm = _FakeLLM(EXTRACTION_JSON)
    pipeline = IngestPipeline(
        s, graph=graph, vectors=vectors, embed=embed, llm=llm, records=records
    )

    # Import the mcp server module
    from katsi_mcp import server as srv

    srv._state.clear()
    srv._state.update(
        {
            "settings": s,
            "embed": embed,
            "llm": llm,
            "graph": graph,
            "vectors": vectors,
            "records": records,
            "pipeline": pipeline,
        }
    )
    return srv, embed, llm, records


def test_index_status_counts_zero_when_empty(server_state):
    srv, embed, llm, records = server_state
    res = srv.index_status()
    assert isinstance(res, dict)
    assert res["total_files"] == 0
    assert res["total_chunks"] == 0
    assert res["last_indexed_at"] is None
    assert res["projection_diagnostics"] == []
    assert res["projection_lag"] is False


def test_status_and_context_expose_authoritative_projection_lag(server_state, tmp_path):
    srv, _embed, _llm, _records = server_state
    database = WorkspaceSQLite(
        tmp_path / "workspace.sqlite3", srv._state["settings"].workspace.sqlite
    )
    with database.connection() as connection:
        apply_migrations(connection, target_version=3)
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Workspace")
    WorkspaceRepository(database).append_event(
        workspace.id,
        workspace.state_version,
        WorkspaceEventKind.RESOURCE_UPDATED,
        projection_payloads={"vector": {"action": "replace"}},
    )
    srv._state["workspace_database"] = database

    status = srv.index_status()
    assert status["projection_lag"] is True
    assert status["projection_diagnostics"] == [
        {
            "workspace_id": str(workspace.id),
            "projection_name": "vector",
            "applied_outbox_id": 0,
            "latest_outbox_id": 1,
            "lag": 1,
            "lagging": True,
        }
    ]
    context = srv.get_context("anything", max_tokens=500)
    assert context.projection_lag is True
    assert context.projection_diagnostics == status["projection_diagnostics"]


def test_get_context_returns_empty_bundle_when_unindexed(server_state):
    srv, embed, llm, records = server_state
    b = srv.get_context("anything", max_tokens=500)
    assert b.query == "anything"
    assert b.files == []
    assert b.chunks == []
    assert b.token_estimate == 0


def test_get_file_summary_unknown_raises(server_state):
    srv, embed, llm, records = server_state
    with pytest.raises(ValueError):
        srv.get_file_summary("nonexistent")


def test_related_returns_empty_for_unknown_file(server_state):
    srv, embed, llm, records = server_state
    out = srv.related("nonexistent")
    assert out == []


def test_answer_tool_disabled_by_default_raises(server_state):
    srv, embed, llm, records = server_state
    # Settings.mcp.enable_answer_tool defaults to False
    with pytest.raises(PermissionError):
        srv.answer("any query")


def test_answer_tool_works_when_enabled(server_state):
    srv, embed, llm, records = server_state
    srv._state["settings"].mcp.enable_answer_tool = True
    out = srv.answer("any query")
    assert isinstance(out, dict)
    assert "text" in out
    assert "mode" in out
    assert "escalated" in out


def test_answer_tool_returns_mode_and_escalated(server_state):
    srv, embed, llm, records = server_state
    srv._state["settings"].mcp.enable_answer_tool = True
    out = srv.answer("q", mode="local")
    assert isinstance(out, dict)
    assert out["mode"] == "local"
    assert out["escalated"] is False
    assert isinstance(out["text"], str)


def test_answer_tool_return_only_when_mode_return_only(server_state):
    srv, embed, llm, records = server_state
    srv._state["settings"].mcp.enable_answer_tool = True
    out = srv.answer("q", mode="return_only")
    assert out["text"] is None
    assert out["mode"] == "return_only"
    assert "hint" in out


def test_answer_tool_override_disabled_raises(server_state):
    srv, embed, llm, records = server_state
    srv._state["settings"].mcp.enable_answer_tool = True
    srv._state["settings"].synth.allow_per_call_override = False
    with pytest.raises(SynthConfigError):
        srv.answer("q", mode="local")


def test_smoke_index_then_get_context(server_state, tmp_path):
    """End-to-end smoke: index a small markdown file via the pipeline, then
    call get_context via the MCP server tool and assert the bundle is non-empty."""
    srv, embed, llm, records = server_state
    md_path = tmp_path / "doc.md"
    md_path.write_text("# Title\n\nThis document mentions Acme and AI.")
    # Index via the pipeline exposed through the server
    rec = srv.index_file_tool(str(md_path))
    assert rec.status.value == "indexed"
    assert embed.calls == 1  # one embed call for the chunks
    assert llm.calls == 1  # one extract call

    # Now get_context should find it
    bundle = srv.get_context("Acme AI", max_tokens=2000)
    assert bundle.query == "Acme AI"
    # at least one file should be in the bundle
    assert len(bundle.files) >= 1
    # second call with same query shouldn't need a new extract (only 1 was made),
    # but embed.embed will be called again for the query vector. Allow that.


# ---------------------------------------------------------------------------
# list_media_representations
#
# The capability-checked media tools had no harness before this: the
# server_state fixture supplies no workspace database, registry, or identity.
# ---------------------------------------------------------------------------


class _FakeIdentity:
    def __init__(self):
        self.id = uuid4()


class _FakeIdentityService:
    def __init__(self, *, allow: bool = True):
        self.allow = allow

    def authorize(self, *args, **kwargs):
        if not self.allow:
            raise PermissionError("denied")


def _scene(resource_version_id, *, start_ms, end_ms):
    representation_id = uuid4()
    now = datetime.now(UTC)
    return DerivedRepresentation(
        id=representation_id,
        resource_version_id=resource_version_id,
        kind=MediaRepresentationKind.SCENE,
        media_type="application/json",
        status=MediaRepresentationStatus.CURRENT,
        created_at=now,
        updated_at=now,
        textual_payload="a caption that must never be disclosed here",
        locators=(
            TimeRangeLocator(
                resource_version_id=resource_version_id,
                representation_id=representation_id,
                start_ms=start_ms,
                end_ms=end_ms,
            ),
        ),
        coverage=MediaCoverage(is_complete=False, coverage_fraction=0.2),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name="fake_scene",
            adapter_version="1",
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash="a" * 64,
            representation_kind=MediaRepresentationKind.SCENE,
            stage=PipelineStage.DETECT_SCENES,
            adapter_name="fake_scene",
            adapter_version="1",
            sampling_fingerprint="b" * 64,
        ),
    )


def _keyframe(resource_version_id):
    representation_id = uuid4()
    now = datetime.now(UTC)
    return DerivedRepresentation(
        id=representation_id,
        resource_version_id=resource_version_id,
        kind=MediaRepresentationKind.KEYFRAME,
        media_type="image/png",
        status=MediaRepresentationStatus.CURRENT,
        created_at=now,
        updated_at=now,
        blob_reference=f"private-keyframe:{'f' * 8}",
        blob_hash="f" * 64,
        blob_byte_count=2048,
        locators=(
            WholeResourceLocator(
                resource_version_id=resource_version_id, representation_id=representation_id
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name="fake_keyframe",
            adapter_version="1",
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash="c" * 64,
            representation_kind=MediaRepresentationKind.KEYFRAME,
            stage=PipelineStage.EXTRACT_KEYFRAMES,
            adapter_name="fake_keyframe",
            adapter_version="1",
            sampling_fingerprint="d" * 64,
        ),
    )


@pytest.fixture
def media_state(server_state, tmp_path):
    """Wire a workspace database, registry, and authorised identity into _state."""
    srv, _embed, _llm, _records = server_state

    workspace_id = str(uuid4())
    resource_id = str(uuid4())
    resource_version_id = ResourceVersionId(str(uuid4()))
    media_path = "/project/clip.mp4"

    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, target_version=1)
        connection.execute(
            "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?)",
            (workspace_id, "/project", "Project", "active", 1, "now", "now"),
        )
        connection.execute(
            "INSERT INTO resources VALUES (?, ?, ?, ?, ?, ?, ?)",
            (resource_id, workspace_id, media_path, "current", 1, "now", "now"),
        )
        connection.execute(
            "INSERT INTO resource_versions VALUES (?, ?, ?, ?, ?, ?)",
            (str(resource_version_id), resource_id, "e" * 64, 1024, "2026-01-01T00:00:00Z", "evt"),
        )

    registry = RepresentationRegistry(database)
    srv._state.update(
        {
            "workspace_database": database,
            "representation_registry": registry,
            "identity_service": _FakeIdentityService(),
            "authenticated_identity": _FakeIdentity(),
        }
    )
    return srv, registry, workspace_id, resource_version_id, media_path


def test_list_media_representations_returns_current_generation(media_state):
    srv, registry, workspace_id, resource_version_id, media_path = media_state
    scenes = [
        _scene(resource_version_id, start_ms=0, end_ms=1000),
        _scene(resource_version_id, start_ms=1000, end_ms=2000),
        _scene(resource_version_id, start_ms=2000, end_ms=3000),
    ]
    registry.register_representation_batch(scenes)

    rows = srv.list_media_representations(workspace_id, media_path)

    assert {row["representation_id"] for row in rows} == {str(s.id) for s in scenes}
    assert [row["locators"][0]["start_ms"] for row in rows] == [0, 1000, 2000]


def test_list_media_representations_never_discloses_payload(media_state):
    srv, registry, workspace_id, resource_version_id, media_path = media_state
    registry.register_representation_batch([_scene(resource_version_id, start_ms=0, end_ms=1000)])

    rows = srv.list_media_representations(workspace_id, media_path)

    assert rows
    for row in rows:
        assert set(row) == {
            "representation_id",
            "resource_version_id",
            "kind",
            "status",
            "locators",
            "coverage_fraction",
        }
        assert "caption" not in json.dumps(row)


def test_list_media_representations_filters_by_kind(media_state):
    srv, registry, workspace_id, resource_version_id, media_path = media_state
    registry.register_representation_batch([_scene(resource_version_id, start_ms=0, end_ms=1000)])
    registry.register_representation_batch([_keyframe(resource_version_id)])

    rows = srv.list_media_representations(workspace_id, media_path, kinds=["scene"])

    assert [row["kind"] for row in rows] == ["scene"]


def test_list_media_representations_omits_superseded_generation(media_state):
    srv, registry, workspace_id, resource_version_id, media_path = media_state
    old = [_scene(resource_version_id, start_ms=0, end_ms=1000)]
    registry.register_representation_batch(old)
    new = [_scene(resource_version_id, start_ms=0, end_ms=500)]
    registry.register_representation_batch(new)

    rows = srv.list_media_representations(workspace_id, media_path)

    assert [row["representation_id"] for row in rows] == [str(new[0].id)]


def test_list_media_representations_is_deterministic(media_state):
    srv, registry, workspace_id, resource_version_id, media_path = media_state
    registry.register_representation_batch(
        [
            _scene(resource_version_id, start_ms=2000, end_ms=3000),
            _scene(resource_version_id, start_ms=0, end_ms=1000),
            _scene(resource_version_id, start_ms=1000, end_ms=2000),
        ]
    )

    first = srv.list_media_representations(workspace_id, media_path)
    second = srv.list_media_representations(workspace_id, media_path)

    assert first == second


def test_list_media_representations_rejects_unknown_kind(media_state):
    srv, _registry, workspace_id, _rv, media_path = media_state

    with pytest.raises(ValueError, match="unknown representation kind: bogus"):
        srv.list_media_representations(workspace_id, media_path, kinds=["bogus"])


@pytest.mark.parametrize("limit", [0, 501])
def test_list_media_representations_rejects_out_of_range_limit(media_state, limit):
    srv, _registry, workspace_id, _rv, media_path = media_state

    with pytest.raises(ValueError, match="limit must be between"):
        srv.list_media_representations(workspace_id, media_path, limit=limit)


def test_list_media_representations_rejects_unknown_path(media_state):
    srv, _registry, workspace_id, _rv, _media_path = media_state

    with pytest.raises(ValueError, match="unknown path in workspace"):
        srv.list_media_representations(workspace_id, "/project/missing.mp4")


def test_list_media_representations_requires_authentication(media_state):
    srv, _registry, workspace_id, _rv, media_path = media_state
    srv._state["authenticated_identity"] = None

    with pytest.raises(PermissionError, match="Authentication required"):
        srv.list_media_representations(workspace_id, media_path)


def test_list_media_representations_requires_capability(media_state):
    srv, _registry, workspace_id, _rv, media_path = media_state
    srv._state["identity_service"] = _FakeIdentityService(allow=False)

    with pytest.raises(PermissionError, match="authorization denied"):
        srv.list_media_representations(workspace_id, media_path)
