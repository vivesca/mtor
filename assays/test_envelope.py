"""Tests for envelope.py — JSON envelope structure validation."""
import json
from io import StringIO
from unittest.mock import patch

from mtor.envelope import _ok, _err, _extract_first_result


def test_ok_envelope_structure() -> None:
    """Test that _ok produces a valid JSON envelope with correct structure."""
    with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
        _ok("test_command", {"key": "value"}, [{"action": "next"}])
        output = mock_stdout.getvalue().strip()
        data = json.loads(output)
        
        # Verify top-level structure
        assert "command" in data
        assert "result" in data
        assert "next_actions" in data
        assert "ok" in data
        assert data["ok"] is True
        
        # Verify content matches input
        assert data["command"] == "test_command"
        assert data["result"] == {"key": "value"}
        assert data["next_actions"] == [{"action": "next"}]


def test_err_envelope_structure() -> None:
    """Test that _err produces a valid error JSON envelope with correct structure."""
    with patch("mtor.envelope.emit_err") as mock_emit:
        exit_code = _err("test_command", "something went wrong", "ERROR_CODE", "fix this", [{"action": "retry"}])
        
        # Verify the function was called with correct arguments
        mock_emit.assert_called_once()
        args = mock_emit.call_args[0]
        assert args[0] == "test_command"
        assert args[1] == "something went wrong"
        assert args[2] == "ERROR_CODE"
        assert args[3] == "fix this"
        assert args[4] == [{"action": "retry"}]
        
        # Verify exit code
        assert exit_code == 1


def test_ok_includes_version() -> None:
    """Test that _ok includes the version string when provided."""
    test_version = "1.2.0"
    
    with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
        _ok("test_command", {"key": "value"}, version=test_version)
        output = mock_stdout.getvalue().strip()
        data = json.loads(output)
        
        assert data["version"] == test_version


def test_err_includes_fix_when_provided() -> None:
    """Test that _err includes the fix hint when provided."""
    with patch("mtor.envelope.emit_err") as mock_emit:
        fix_text = "Run `mtor init` to fix this"
        _err("test_command", "init required", "MISSING_INIT", fix_text)
        
        mock_emit.assert_called_once()
        args = mock_emit.call_args[0]
        assert args[3] == fix_text


def test_extract_first_result_from_batch() -> None:
    """Test _extract_first_result extracts from batch envelope correctly."""
    wf_result = {
        "results": [
            {"exit_code": 0, "output": "first"},
            {"exit_code": 1, "output": "second"}
        ]
    }
    result = _extract_first_result(wf_result)
    assert result == {"exit_code": 0, "output": "first"}


def test_extract_first_result_flat() -> None:
    """Test _extract_first_result handles flat (non-batch) result correctly."""
    wf_result = {"exit_code": 0, "output": "flat result"}
    result = _extract_first_result(wf_result)
    assert result == wf_result


def test_extract_first_result_empty() -> None:
    """Test _extract_first_result returns None when no results present."""
    wf_result = {"some_other_key": "value"}
    result = _extract_first_result(wf_result)
    assert result is None
