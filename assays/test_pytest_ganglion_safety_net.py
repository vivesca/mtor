import subprocess

import pytest

import mtor.infra


def test_ganglion_safety_net_blocks_ssh_ganglion_argv():
    with pytest.raises(RuntimeError) as excinfo:
        subprocess.run(["ssh", "ganglion", "echo", "hi"])
    assert "ganglion" in str(excinfo.value)


def test_ganglion_safety_net_blocks_mtor_worker_restart_argv():
    with pytest.raises(RuntimeError) as excinfo:
        subprocess.run(
            ["ssh", "somehost", "systemctl", "--user", "restart", "mtor-worker"]
        )
    assert "mtor-worker" in str(excinfo.value)


def test_ganglion_safety_net_allows_unrelated_subprocess_calls():
    result = subprocess.run(["echo", "hello"], capture_output=True, text=True)
    assert result.stdout.strip() == "hello"


def test_ganglion_safety_net_blocks_via_module_qualified_reference():
    with pytest.raises(RuntimeError) as excinfo:
        mtor.infra.subprocess.run(
            ["ssh", "ganglion", "systemctl", "--user", "restart", "mtor-worker"]
        )
    assert "ganglion" in str(excinfo.value)
