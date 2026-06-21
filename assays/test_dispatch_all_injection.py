"""Regression: `mtor dispatch-all` must not double-inject spec constraints.

dispatch-all builds an injected `prompt` for the dry-run preview, but the real
dispatch must hand the UN-injected base prompt to `_dispatch_prompt` — which
injects exactly once when spec_path is set. Passing the already-injected prompt
doubled the CONSTRAINT/Run block and diverged the workflow-ID + dedup hash from
the normal `mtor --spec` path.
"""

from pathlib import Path

from mtor import cli
from mtor.rptor import parse_spec


def _write_spec(tmp_path: Path) -> dict:
    spec_file = tmp_path / "demo.md"
    spec_file.write_text(
        "---\n"
        "status: ready\n"
        "scope: foo.py\n"
        "repo: ~\n"
        "tests:\n"
        "  run: pytest assays/test_demo.py\n"
        "---\n"
        "Do the thing.\n",
        encoding="utf-8",
    )
    return parse_spec(spec_file)


def test_dispatch_all_passes_uninjected_prompt(tmp_path, monkeypatch, capsys):
    """The prompt handed to _dispatch_prompt is the base body (injection deferred),
    not the already-injected preview — so constraints are applied exactly once."""
    spec = _write_spec(tmp_path)

    # Stub the selection pipeline so our single real spec is the only candidate.
    monkeypatch.setattr(cli, "scan_specs", lambda _d: [spec])
    monkeypatch.setattr(cli, "resolve_dag", lambda s: s)
    monkeypatch.setattr(cli, "topological_sort", lambda s: s)
    monkeypatch.setattr(cli, "_select_dispatch_candidates", lambda resolved, repo: (list(resolved), []))

    captured: dict = {}

    def fake_dispatch_prompt(prompt, *, provider=None, spec_path=None, **kw):
        captured["prompt"] = prompt
        captured["spec_path"] = spec_path
        return "wf-test-id"

    monkeypatch.setattr(cli, "_dispatch_prompt", fake_dispatch_prompt)

    cli.dispatch_all(dir=tmp_path, dry_run=False)

    # The real injection (reading the spec file) would add this marker; the base
    # prompt handed to _dispatch_prompt must NOT already contain it.
    assert "CONSTRAINT: Only modify" not in captured["prompt"]
    assert captured["prompt"].strip() == "Do the thing."
    # spec_path must still be passed so _dispatch_prompt injects exactly once.
    assert Path(captured["spec_path"]) == Path(spec["path"])
