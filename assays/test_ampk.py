"""Tests for AMPK sensing — ganglion load monitoring and dispatch throttling.

AMPK is the cellular energy sensor. When load is high on the ganglion
(remote worker), dispatch must be blocked to prevent overloading.

Run: cd ~/code/mtor && uv run pytest assays/test_ampk.py -v
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# test_check_ganglion_load
# ---------------------------------------------------------------------------


def test_check_ganglion_load():
    """check_ganglion_load SSHes to worker, parses tasks + load average."""
    from mtor.watch import GanglionLoad, check_ganglion_load

    mock_result = MagicMock()
    mock_result.stdout = "2\n1.25\n"
    mock_result.returncode = 0

    with patch("mtor.watch.subprocess.run", return_value=mock_result) as mock_run:
        load = check_ganglion_load(worker_host="testhost")

    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "ssh"
    assert cmd[1] == "testhost"

    assert isinstance(load, GanglionLoad)
    assert load.running_tasks == 2
    assert load.load_avg == 1.25
    assert load.load_level == "low"


def test_check_ganglion_load_medium():
    """Medium load when tasks or load_avg cross lower thresholds."""
    from mtor.watch import GanglionLoad, check_ganglion_load

    mock_result = MagicMock()
    mock_result.stdout = "4\n2.5\n"
    mock_result.returncode = 0

    with patch("mtor.watch.subprocess.run", return_value=mock_result):
        load = check_ganglion_load(worker_host="testhost")

    assert load.load_level == "medium"


def test_check_ganglion_load_high():
    """High load when tasks or load_avg cross upper thresholds."""
    from mtor.watch import GanglionLoad, check_ganglion_load

    mock_result = MagicMock()
    mock_result.stdout = "8\n5.1\n"
    mock_result.returncode = 0

    with patch("mtor.watch.subprocess.run", return_value=mock_result):
        load = check_ganglion_load(worker_host="testhost")

    assert load.load_level == "high"


def test_check_ganglion_load_ssh_failure():
    """SSH failure returns high-load sentinel (fail-closed)."""
    from mtor.watch import GanglionLoad, check_ganglion_load

    mock_result = MagicMock()
    mock_result.stdout = ""
    mock_result.returncode = 1

    with patch("mtor.watch.subprocess.run", return_value=mock_result):
        load = check_ganglion_load(worker_host="testhost")

    assert load.load_level == "high"
    assert load.running_tasks == 0


def test_check_ganglion_load_timeout():
    """SSH timeout returns high-load sentinel."""
    from mtor.watch import check_ganglion_load

    with patch(
        "mtor.watch.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=10),
    ):
        load = check_ganglion_load(worker_host="testhost")

    assert load.load_level == "high"


def test_check_ganglion_load_defaults_to_worker_host(monkeypatch):
    """Without worker_host arg, falls back to WORKER_HOST from config."""
    from mtor.watch import check_ganglion_load

    monkeypatch.setattr("mtor.WORKER_HOST", "fallback-host")

    mock_result = MagicMock()
    mock_result.stdout = "0\n0.1\n"
    mock_result.returncode = 0

    with patch("mtor.watch.subprocess.run", return_value=mock_result) as mock_run:
        load = check_ganglion_load()

    assert mock_run.call_args[0][0][1] == "fallback-host"
    assert load.load_level == "low"


# ---------------------------------------------------------------------------
# test_dispatch_blocked_when_load_high
# ---------------------------------------------------------------------------


def test_dispatch_blocked_when_load_high():
    """should_block_dispatch returns True when ganglion load exceeds thresholds."""
    from mtor.watch import GanglionLoad
    from mtor.worker.provider import should_block_dispatch

    high_load = GanglionLoad(running_tasks=8, load_avg=5.5, load_level="high")
    assert should_block_dispatch(high_load) is True

    # Also blocks on tasks alone (below load_avg threshold)
    high_tasks = GanglionLoad(running_tasks=7, load_avg=1.0, load_level="medium")
    assert should_block_dispatch(high_tasks) is True

    # Also blocks on load_avg alone (below tasks threshold)
    high_loadavg = GanglionLoad(running_tasks=1, load_avg=6.0, load_level="high")
    assert should_block_dispatch(high_loadavg) is True


def test_dispatch_not_blocked_when_load_low():
    """should_block_dispatch returns False when ganglion load is fine."""
    from mtor.watch import GanglionLoad
    from mtor.worker.provider import should_block_dispatch

    low_load = GanglionLoad(running_tasks=1, load_avg=0.5, load_level="low")
    assert should_block_dispatch(low_load) is False


def test_dispatch_blocked_custom_thresholds():
    """should_block_dispatch respects custom max_tasks and max_load_avg."""
    from mtor.watch import GanglionLoad
    from mtor.worker.provider import should_block_dispatch

    load = GanglionLoad(running_tasks=3, load_avg=1.5, load_level="medium")

    # Default thresholds: not blocked
    assert should_block_dispatch(load) is False

    # Lower thresholds: blocked
    assert should_block_dispatch(load, max_tasks=2) is True
    assert should_block_dispatch(load, max_load_avg=1.0) is True


def test_dispatch_blocked_graceful_on_bad_input():
    """should_block_dispatch returns False for inputs missing required attrs."""
    from mtor.worker.provider import should_block_dispatch

    assert should_block_dispatch(None) is False
    assert should_block_dispatch("not_a_load") is False
