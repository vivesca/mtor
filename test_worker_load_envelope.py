"""Queue-wait visibility: dispatch envelope reports current worker load."""

from mtor.dispatch import _dispatch_explanation, _worker_load_plan


def test_worker_load_idle():
    plan = _worker_load_plan(count_running=lambda: 0)
    assert plan["running"] == 0
    assert "idle" in plan["detail"]


def test_worker_load_busy():
    plan = _worker_load_plan(count_running=lambda: 3)
    assert plan["running"] == 3
    assert "3" in plan["detail"]


def test_worker_load_unavailable_never_blocks():
    def boom():
        raise RuntimeError("temporal down")

    plan = _worker_load_plan(count_running=boom)
    assert plan["running"] is None
    assert plan["ok"] is True


def test_explanation_includes_worker_load(monkeypatch):
    import mtor.dispatch as dispatch_mod

    monkeypatch.setattr(dispatch_mod, "_dedup_plan", lambda *a, **k: {})
    monkeypatch.setattr(
        dispatch_mod,
        "_worker_load_plan",
        lambda **k: {
            "running": 2,
            "ok": True,
            "detail": "queued behind 2 running task(s)",
        },
    )
    plan = _dispatch_explanation("summarise reuse tradeoffs", mode="research")
    assert plan["worker_load"]["running"] == 2
