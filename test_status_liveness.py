"""Liveness in `mtor status`: surface the worker heartbeat so RUNNING is inspectable."""

import json

from mtor.cli import _liveness_from_log_tail

HB = (
    'RIBOSOME_PROGRESS:{"ts": "%s", "event": "heartbeat", "phase": "running",'
    ' "detail": "worker_pid=1 elapsed=%ds output_bytes=%d stale_for=%ds"}'
)


def _hb_line(ts, elapsed, out_bytes, stale):
    line = HB % (ts, elapsed, out_bytes, stale)
    json.loads(line.split("RIBOSOME_PROGRESS:", 1)[1])  # stays valid JSON
    return line


def test_active_heartbeat():
    tail = "\n".join(
        [
            "noise line",
            _hb_line("2026-07-11T06:47:00Z", 600, 100000, 10),
        ]
    )
    plan = _liveness_from_log_tail(tail, now_ts="2026-07-11T06:47:30Z")
    assert plan["state"] == "active"
    assert plan["elapsed_seconds"] == 600
    assert plan["output_bytes"] == 100000
    assert plan["heartbeat_age_seconds"] == 30


def test_stalled_heartbeat():
    tail = _hb_line("2026-07-11T06:30:00Z", 300, 5000, 280)
    plan = _liveness_from_log_tail(tail, now_ts="2026-07-11T06:40:00Z")
    assert plan["state"] == "stalled"
    assert plan["heartbeat_age_seconds"] == 600


def test_no_heartbeat_lines():
    plan = _liveness_from_log_tail("just some output\nno progress markers")
    assert plan["state"] == "unknown"
    assert plan["heartbeat_age_seconds"] is None


def test_malformed_heartbeat_json_is_unknown():
    plan = _liveness_from_log_tail('RIBOSOME_PROGRESS:{"event": "heartbeat", truncated')
    assert plan["state"] == "unknown"
