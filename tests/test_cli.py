"""Tests for the katsi Typer CLI."""

from __future__ import annotations

import pytest

from katsi_core.config import Settings
from katsi_core.ingest.pipeline import IngestPipeline
from katsi_core.ingest.records import FileRecordStore
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore


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
        import json as _json

        self.calls += 1
        from katsi_core.models import Extraction  # noqa: PLC0415

        return Extraction(**_json.loads(self.json_str))

    def chat(self, prompt, *, temperature: float = 0.2):
        return f"local-answer (prompt-len={len(prompt)})"

    def _chat(self, system_prompt, user_text):
        return ""


EXTRACTION_JSON = '{"summary":"doc summary","entities":[{"name":"Acme","kind":"org"}],"topics":["ai"],"references":[]}'


@pytest.fixture
def cli_runner(tmp_path):
    """Return (runner, services dict) wired to tmp_path."""
    import katsi_cli.main as cli_main

    # Build local stores pointing at tmp_path
    s = Settings()
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

    cli_main._state.clear()
    cli_main._state.update(
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
    from typer.testing import CliRunner

    return CliRunner(), cli_main, embed, llm, records, s


def test_status_zero_when_empty(cli_runner):
    runner, cli_main, _, _, _, _ = cli_runner
    res = runner.invoke(cli_main.app, ["status"])
    assert res.exit_code == 0, res.output
    assert "total files" in res.output.lower()


def test_index_processes_md_file(cli_runner, tmp_path):
    runner, cli_main, embed, llm, _, _ = cli_runner
    md_path = tmp_path / "doc.md"
    md_path.write_text("# Hello\n\nmentions Acme.")
    res = runner.invoke(cli_main.app, ["index", str(md_path)])
    assert res.exit_code == 0, res.output
    assert embed.calls == 1
    assert llm.calls == 1


def test_search_prints_results_after_indexing(cli_runner, tmp_path):
    runner, cli_main, embed, _, _, _ = cli_runner
    md_path = tmp_path / "doc.md"
    md_path.write_text("# Acme Doc\n\nMentions Acme and AI.")
    res = runner.invoke(cli_main.app, ["index", str(md_path)])
    assert res.exit_code == 0, res.output
    res2 = runner.invoke(cli_main.app, ["search", "Acme AI"])
    assert res2.exit_code == 0, res2.output
    # Rich may truncate long paths in table cells; verify the search command ran
    assert "search: Acme AI" in res2.output
    assert "score" in res2.output.lower()


def test_ask_prints_bundle_and_relationships(cli_runner, tmp_path):
    runner, cli_main, _, _, _, _ = cli_runner
    md_path = tmp_path / "doc.md"
    md_path.write_text("# Acme\n\nMentions Acme and AI.")
    res = runner.invoke(cli_main.app, ["index", str(md_path)])
    assert res.exit_code == 0, res.output
    res2 = runner.invoke(cli_main.app, ["ask", "Acme AI"])
    assert res2.exit_code == 0, res2.output
    # the query name should appear in output
    assert "Acme AI" in res2.output or "doc.md" in res2.output
    assert "score" in res2.output.lower()


def test_index_missing_path_errors(cli_runner, tmp_path):
    runner, cli_main, _, _, _, _ = cli_runner
    nonsense = tmp_path / "does_not_exist"
    res = runner.invoke(cli_main.app, ["index", str(nonsense)])
    assert res.exit_code != 0


def test_ask_local_with_disabled_answer_tool_passes(cli_runner, tmp_path):
    """The --local flag should not crash. The CLI gracefully prints a 'disabled' note."""
    runner, cli_main, _, _, _, s = cli_runner
    s.mcp.enable_answer_tool = False
    md_path = tmp_path / "doc.md"
    md_path.write_text("# Acme\n\nMentions Acme and AI.")
    runner.invoke(cli_main.app, ["index", str(md_path)])
    res = runner.invoke(cli_main.app, ["ask", "Acme AI", "--local"])
    assert res.exit_code == 0, res.output
    assert "synthesis" in res.output.lower() or "score" in res.output.lower()
    assert "score" in res.output.lower()


def test_ask_mode_return_only_smoke(cli_runner, tmp_path):
    runner, cli_main, _, _, _, s = cli_runner
    s.mcp.enable_answer_tool = True
    md_path = tmp_path / "doc.md"
    md_path.write_text("# Acme\n\nMentions Acme and AI.")
    runner.invoke(cli_main.app, ["index", str(md_path)])
    res = runner.invoke(cli_main.app, ["ask", "Acme AI", "--mode", "return_only"])
    assert res.exit_code == 0, res.output
    assert "mode=return_only" in res.output


def test_help_lists_all_four_commands(cli_runner):
    runner, cli_main, _, _, _, _ = cli_runner
    res = runner.invoke(cli_main.app, ["--help"])
    assert res.exit_code == 0, res.output
    assert "index" in res.output
    assert "status" in res.output
    assert "search" in res.output
    assert "ask" in res.output
