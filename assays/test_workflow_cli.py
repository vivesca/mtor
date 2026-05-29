"""Tests for mtor workflow CLI commands — assess and init."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from mtor.cli import app


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

    output = captured.getvalue()
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"Output is not valid JSON. Exit={exit_code}\nOutput: {output!r}\nException: {exc}"
        ) from exc
    return exit_code, data


def test_workflow_assess_returns_controls_for_high_cost_work() -> None:
    """High-cost work prompt returns workflow route with required controls."""
    exit_code, result = invoke(["workflow", "assess", "High-cost critical work before merge"])
    assert exit_code == 0
    assert result["ok"] is True
    assert result["result"]["route"] == "workflow"
    assert "high_cost_decision" in result["result"]["triggers"]
    assert "resumable_ledger" in result["result"]["required_controls"]
    assert "independent_verification" in result["result"]["required_controls"]


def test_workflow_init_writes_ledger(tmp_path: Path) -> None:
    """workflow init creates a ledger file on disk."""
    exit_code, result = invoke(
        ["workflow", "init", "Security audit and hardening", "--dir", str(tmp_path)]
    )
    assert exit_code == 0
    assert result["ok"] is True
    assert result["result"]["dry_run"] is False
    ledger_path = Path(result["result"]["path"])
    assert ledger_path.exists()
    content = ledger_path.read_text()
    assert "Security audit and hardening" in content


def test_workflow_init_dry_run_does_not_write(tmp_path: Path) -> None:
    """workflow init --dry-run reports path but does not write files."""
    exit_code, result = invoke(
        ["workflow", "init", "Migration and modernization", "--dir", str(tmp_path), "--dry-run"]
    )
    assert exit_code == 0
    assert result["ok"] is True
    assert result["result"]["dry_run"] is True
    assert "Would write ledger" in result["result"]["message"]
    ledger_path = Path(result["result"]["path"])
    assert not ledger_path.exists()
