"""Tests for mtor plan DAG phase 2 — topological dispatch, search attributes, list filters."""

from __future__ import annotations

import io
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from mtor.cli import app
from mtor.plan import topological_sort


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def invoke(args: list[str]) -> tuple[int, dict]:
    """Invoke CLI and return (exit_code, parsed_json)."""
    captured = io.StringIO()
    old_stdout = sys.stdout
    exit_code = 0
    try:
        sys.stdout = captured
        app(args)
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdout = old_stdout
    return exit_code, json.loads(captured.getvalue())


def _write_spec(tmp_path, name, frontmatter, body=""):
    """Write a spec .md file with the given frontmatter and body."""
    (tmp_path / f"{name}.md").write_text(
        f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8"
    )


_CLIENT_PATCH_TARGETS = [
    "mtor.cli._get_client",
    "mtor.doctor._get_client",
    "mtor.dispatch._get_client",
]


def _make_mock_client():
    """Build a minimal mock Temporal client."""
    client = MagicMock()
    handle = MagicMock()
    handle.id = "test-wf-001"
    client.start_workflow = AsyncMock(return_value=handle)

    async def _empty_list(query=None):
        return
        yield  # make this an async generator  # noqa: unreachable

    client.list_workflows = _empty_list
    return client, handle


def _patch_client(mock_client):
    """Patch _get_client across all modules that import it."""
    stack = ExitStack()
    for target in _CLIENT_PATCH_TARGETS:
        stack.enter_context(patch(target, return_value=(mock_client, None)))
    return stack


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTopologicalSort:
    def test_linear_chain(self):
        """a→b→c sorted so a comes first."""
        specs = [
            {"name": "c", "depends_on": ["b"]},
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        result = topological_sort(specs)
        names = [s["name"] for s in result]
        assert names == ["a", "b", "c"]

    def test_diamond(self):
        """Diamond DAG: a→b, a→c, b→d, c→d. d must come last."""
        specs = [
            {"name": "d", "depends_on": ["b", "c"]},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["a"]},
            {"name": "a", "depends_on": []},
        ]
        result = topological_sort(specs)
        names = [s["name"] for s in result]
        assert names.index("a") < names.index("b")
        assert names.index("a") < names.index("c")
        assert names.index("b") < names.index("d")
        assert names.index("c") < names.index("d")

    def test_independent_specs_alphabetical(self):
        """Specs with no inter-dependencies sorted alphabetically."""
        specs = [
            {"name": "zeta", "depends_on": []},
            {"name": "alpha", "depends_on": []},
            {"name": "mid", "depends_on": []},
        ]
        result = topological_sort(specs)
        names = [s["name"] for s in result]
        assert names == ["alpha", "mid", "zeta"]

    def test_deps_outside_set_ignored(self):
        """Dependencies on names not in the input set are treated as satisfied."""
        specs = [
            {"name": "child", "depends_on": ["external"]},
            {"name": "sibling", "depends_on": []},
        ]
        result = topological_sort(specs)
        names = [s["name"] for s in result]
        assert names == ["child", "sibling"]


class TestDispatchAllTopologicalOrder:
    def test_test_dispatch_all_topological_order(self, tmp_path):
        """dispatch-all --dry-run returns specs in topological (dependency) order."""
        _write_spec(tmp_path, "a", "status: done")
        _write_spec(tmp_path, "b", "status: done\ndepends_on:\n  - a")
        _write_spec(tmp_path, "d", "status: done")
        # e depends on d (done) → dispatchable
        _write_spec(tmp_path, "e", "status: ready\ndepends_on:\n  - d", body="Build e.")
        # c depends on b (done) → dispatchable
        _write_spec(tmp_path, "c", "status: ready\ndepends_on:\n  - b", body="Build c.")

        exit_code, data = invoke(["dispatch-all", "--dry-run", "--dir", str(tmp_path)])

        assert exit_code == 0
        assert data["ok"] is True
        names = [s["name"] for s in data["result"]["dispatched"]]
        # Both dispatchable, no inter-dispatchable deps → alphabetical: c, e
        assert names == ["c", "e"]

    def test_test_dispatch_all_dry_run(self, tmp_path):
        """dispatch-all --dry-run lists specs without connecting to Temporal."""
        _write_spec(tmp_path, "a", "status: done")
        _write_spec(tmp_path, "b", "status: ready\ndepends_on:\n  - a", body="Build b.")

        exit_code, data = invoke(["dispatch-all", "--dry-run", "--dir", str(tmp_path)])

        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["dry_run"] is True
        assert len(data["result"]["dispatched"]) == 1
        assert data["result"]["dispatched"][0]["name"] == "b"
        assert data["result"]["dispatched"][0]["status"] == "would_dispatch"


class TestSearchAttributeSetOnDispatch:
    def test_test_search_attribute_set_on_dispatch(self, tmp_path):
        """dispatch-all passes provider to each dispatched workflow spec."""
        _write_spec(tmp_path, "a", "status: done")
        _write_spec(tmp_path, "b", "status: ready\ndepends_on:\n  - a", body="Build b.")

        with patch("mtor.cli._dispatch_prompt", return_value="wf-123") as mock_dispatch:
            exit_code, data = invoke(
                ["dispatch-all", "--dir", str(tmp_path), "-p", "goose"]
            )

        assert exit_code == 0
        assert data["ok"] is True
        mock_dispatch.assert_called_once()
        assert mock_dispatch.call_args.kwargs["provider"] == "goose"


class TestListFilterByProvider:
    def test_test_list_filter_by_provider(self, tmp_path, monkeypatch):
        """mtor list --provider filters workflows by provider search attribute."""
        import mtor.triage as triage_mod

        monkeypatch.setattr(triage_mod, "TRIAGE_PATH", tmp_path / "triage.json")

        mock_client, _ = _make_mock_client()

        async def _fake_list(query=None):
            for wf_id, prov in [("wf-goose", "goose"), ("wf-zhipu", "zhipu")]:
                execution = MagicMock()
                execution.id = wf_id
                execution.status = MagicMock()
                execution.status.name = "COMPLETED"
                execution.start_time = MagicMock()
                execution.start_time.isoformat.return_value = (
                    "2026-04-06T00:00:00+00:00"
                )
                execution.close_time = MagicMock()
                execution.close_time.isoformat.return_value = (
                    "2026-04-06T00:01:00+00:00"
                )
                execution.search_attributes = {
                    "TranslationProvider": [prov],
                    "TranslationVerdict": ["approved"],
                }
                yield execution

        mock_client.list_workflows = _fake_list

        with _patch_client(mock_client):
            exit_code, data = invoke(["list", "--provider", "goose"])

        assert exit_code == 0
        assert data["ok"] is True
        wf_ids = [wf["workflow_id"] for wf in data["result"]["workflows"]]
        assert "wf-goose" in wf_ids
        assert "wf-zhipu" not in wf_ids


class TestListFilterByVerdict:
    def test_test_list_filter_by_verdict(self, tmp_path, monkeypatch):
        """mtor list --verdict filters workflows by verdict search attribute."""
        import mtor.triage as triage_mod

        monkeypatch.setattr(triage_mod, "TRIAGE_PATH", tmp_path / "triage.json")

        mock_client, _ = _make_mock_client()

        async def _fake_list(query=None):
            for wf_id, vrd in [("wf-approved", "approved"), ("wf-rejected", "rejected")]:
                execution = MagicMock()
                execution.id = wf_id
                execution.status = MagicMock()
                execution.status.name = "COMPLETED"
                execution.start_time = MagicMock()
                execution.start_time.isoformat.return_value = (
                    "2026-04-06T00:00:00+00:00"
                )
                execution.close_time = MagicMock()
                execution.close_time.isoformat.return_value = (
                    "2026-04-06T00:01:00+00:00"
                )
                execution.search_attributes = {
                    "TranslationProvider": ["zhipu"],
                    "TranslationVerdict": [vrd],
                }
                yield execution

        mock_client.list_workflows = _fake_list

        with _patch_client(mock_client):
            exit_code, data = invoke(["list", "--verdict", "approved"])

        assert exit_code == 0
        assert data["ok"] is True
        wf_ids = [wf["workflow_id"] for wf in data["result"]["workflows"]]
        assert "wf-approved" in wf_ids
        assert "wf-rejected" not in wf_ids
