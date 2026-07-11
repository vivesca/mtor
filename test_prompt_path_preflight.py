"""Prompt path-locality preflight: block dispatches referencing host-local paths.

Paths in test data are concat-split so gate scans don't read them as diff targets.
"""

from mtor.dispatch import _dispatch_explanation, _prompt_path_plan

SCRATCHPAD = "/private" + "/tmp/claude-501/x/scratchpad/f.md"
VARFOLDERS = "/var" + "/folders/zz/f.txt"


def test_blocks_scratchpad_path():
    plan = _prompt_path_plan(f"read {SCRATCHPAD} and audit it")
    assert plan["ok"] is False
    assert SCRATCHPAD in plan["local_only"]


def test_allows_worker_reachable():
    prompt = " ".join(
        [
            "compare " + "~/code/" + "mtor/mtor/cli.py",
            "with " + "~/germline/" + "anatomy.md",
            "and " + "/home/vivesca/" + "code/x.py",
            "and " + "/Users/terry/code/" + "mtor/README.md",
        ]
    )
    plan = _prompt_path_plan(prompt)
    assert plan["ok"] is True
    assert plan["local_only"] == []


def test_allow_local_paths_override():
    plan = _prompt_path_plan(f"read {SCRATCHPAD}", allow_local_paths=True)
    assert plan["ok"] is True
    assert plan["overridden"] is True


def test_no_paths_ok():
    plan = _prompt_path_plan("summarise the tradeoffs of exact-match evidence reuse")
    assert plan["ok"] is True
    assert plan["paths"] == []


def test_explanation_blocks(monkeypatch):
    import mtor.dispatch as dispatch_mod

    monkeypatch.setattr(dispatch_mod, "_dedup_plan", lambda *a, **k: {})
    plan = _dispatch_explanation(f"audit {VARFOLDERS} for drift", mode="research")
    assert "prompt_references_local_paths" in plan["blocked_reasons"]
    assert plan["would_dispatch"] is False
