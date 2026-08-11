"""Smoke tests for the katsi FastMCP server tools."""

from __future__ import annotations

import json

import pytest

from katsi_core.config import Settings
from katsi_core.ingest.pipeline import IngestPipeline
from katsi_core.ingest.records import FileRecordStore
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
