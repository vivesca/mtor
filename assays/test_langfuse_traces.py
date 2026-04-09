"""Tests for Langfuse trace enrichment in stall_trace.

Validates that create_task_trace and finalize_trace capture all
relevant execution metadata and handle missing Langfuse gracefully.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch



from mtor.worker.stall_trace import create_task_trace, finalize_trace, get_langfuse


# ---------------------------------------------------------------------------
# get_langfuse
# ---------------------------------------------------------------------------

class TestGetLangfuse:
    """Langfuse client acquisition."""

    def test_returns_module_when_installed(self):
        mock_langfuse = MagicMock()
        with patch("mtor.worker.stall_trace.get_langfuse", return_value=mock_langfuse):
            assert get_langfuse() is mock_langfuse

    @patch("mtor.worker.stall_trace.get_langfuse", return_value=None)
    def test_returns_none_when_missing(self, _mock):
        assert get_langfuse() is None


# ---------------------------------------------------------------------------
# create_task_trace
# ---------------------------------------------------------------------------

class TestCreateTaskTrace:
    """Trace creation with enriched metadata."""

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_returns_trace_object(self, mock_lf):
        mock_trace = MagicMock()
        mock_lf.return_value.trace.return_value = mock_trace

        result = create_task_trace("do the thing", "glm-5.1", "wf-123")

        assert result is mock_trace

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_trace_name_includes_workflow_id(self, mock_lf):
        mock_trace = MagicMock()
        mock_lf.return_value.trace.return_value = mock_trace

        create_task_trace("task", "provider-a", "wf-abc")

        call_kwargs = mock_lf.return_value.trace.call_args[1]
        assert call_kwargs["name"] == "ribosome-wf-abc"

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_trace_metadata_has_provider_and_task(self, mock_lf):
        mock_trace = MagicMock()
        mock_lf.return_value.trace.return_value = mock_trace

        create_task_trace("implement feature X", "deepseek-v3", "wf-1")

        meta = mock_lf.return_value.trace.call_args[1]["metadata"]
        assert meta["provider"] == "deepseek-v3"
        assert meta["task"] == "implement feature X"
        assert meta["workflow_id"] == "wf-1"

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_trace_metadata_includes_input(self, mock_lf):
        """Trace metadata should capture the full task prompt as input."""
        mock_trace = MagicMock()
        mock_lf.return_value.trace.return_value = mock_trace

        create_task_trace("refactor the translocase module", "glm-5.1", "wf-2")

        meta = mock_lf.return_value.trace.call_args[1]["metadata"]
        assert "input" in meta
        assert meta["input"] == "refactor the translocase module"

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_trace_includes_tags(self, mock_lf):
        """Trace should carry tags for provider and status filtering."""
        mock_trace = MagicMock()
        mock_lf.return_value.trace.return_value = mock_trace

        create_task_trace("task", "ark-code", "wf-3")

        call_kwargs = mock_lf.return_value.trace.call_args[1]
        tags = call_kwargs.get("tags", [])
        assert "ark-code" in tags

    def test_returns_none_when_langfuse_unavailable(self):
        with patch("mtor.worker.stall_trace.get_langfuse", return_value=None):
            result = create_task_trace("task", "provider", "wf")
            assert result is None

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_returns_none_on_langfuse_exception(self, mock_lf):
        mock_lf.return_value.trace.side_effect = RuntimeError("boom")

        result = create_task_trace("task", "provider", "wf")
        assert result is None


# ---------------------------------------------------------------------------
# finalize_trace
# ---------------------------------------------------------------------------

class TestFinalizeTrace:
    """Trace finalization captures full result metadata."""

    def test_noop_when_trace_is_none(self):
        """Should not raise when trace is None."""
        finalize_trace(None, {"stdout": "ok", "exit_code": 0})

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_captures_stdout(self, mock_lf):
        mock_trace = MagicMock()
        mock_lf.return_value.flush = MagicMock()

        finalize_trace(mock_trace, {
            "stdout": "all done",
            "exit_code": 0,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": True,
            "stderr": "",
        })

        mock_trace.generation.assert_called_once()
        gen_kwargs = mock_trace.generation.call_args[1]
        assert gen_kwargs["output"] == "all done"

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_metadata_has_exit_code(self, mock_lf):
        mock_trace = MagicMock()
        mock_lf.return_value.flush = MagicMock()

        finalize_trace(mock_trace, {
            "stdout": "out",
            "exit_code": 42,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": False,
            "stderr": "oops",
        })

        meta = mock_trace.generation.call_args[1]["metadata"]
        assert meta["exit_code"] == 42

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_metadata_captures_success(self, mock_lf):
        """Metadata should include success boolean for filtering."""
        mock_trace = MagicMock()
        mock_lf.return_value.flush = MagicMock()

        finalize_trace(mock_trace, {
            "stdout": "ok",
            "exit_code": 0,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": True,
            "stderr": "",
        })

        meta = mock_trace.generation.call_args[1]["metadata"]
        assert meta["success"] is True

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_metadata_captures_stderr(self, mock_lf):
        """Stderr is critical for debugging failures."""
        mock_trace = MagicMock()
        mock_lf.return_value.flush = MagicMock()

        finalize_trace(mock_trace, {
            "stdout": "",
            "exit_code": 1,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": False,
            "stderr": "SyntaxError: invalid syntax",
        })

        meta = mock_trace.generation.call_args[1]["metadata"]
        assert "SyntaxError" in meta["stderr"]

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_metadata_captures_mode(self, mock_lf):
        """Execution mode (scout/normal) aids trace filtering."""
        mock_trace = MagicMock()
        mock_lf.return_value.flush = MagicMock()

        finalize_trace(mock_trace, {
            "stdout": "ok",
            "exit_code": 0,
            "provider": "deepseek-v3",
            "cost_info": "",
            "success": True,
            "stderr": "",
            "mode": "scout",
        })

        meta = mock_trace.generation.call_args[1]["metadata"]
        assert meta["mode"] == "scout"

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_metadata_captures_branch_and_merge(self, mock_lf):
        """Branch name and merge status for tracking worktree outcomes."""
        mock_trace = MagicMock()
        mock_lf.return_value.flush = MagicMock()

        finalize_trace(mock_trace, {
            "stdout": "ok",
            "exit_code": 0,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": True,
            "stderr": "",
            "branch_name": "ribosome-abc123",
            "merged": True,
        })

        meta = mock_trace.generation.call_args[1]["metadata"]
        assert meta["branch_name"] == "ribosome-abc123"
        assert meta["merged"] is True

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_includes_model_field(self, mock_lf):
        """Generation span should carry model=provider for Langfuse UI."""
        mock_trace = MagicMock()
        mock_lf.return_value.flush = MagicMock()

        finalize_trace(mock_trace, {
            "stdout": "ok",
            "exit_code": 0,
            "provider": "ark-code",
            "cost_info": "",
            "success": True,
            "stderr": "",
        })

        gen_kwargs = mock_trace.generation.call_args[1]
        assert gen_kwargs["model"] == "ark-code"

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_includes_input_from_task(self, mock_lf):
        """Generation span should carry the original task as input."""
        mock_trace = MagicMock()
        mock_lf.return_value.flush = MagicMock()

        finalize_trace(mock_trace, {
            "stdout": "ok",
            "exit_code": 0,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": True,
            "stderr": "",
            "task": "fix the bug in parser",
        })

        gen_kwargs = mock_trace.generation.call_args[1]
        assert "fix the bug in parser" in gen_kwargs["input"]

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_includes_status_message(self, mock_lf):
        """Generation span should have status_message reflecting outcome."""
        mock_trace = MagicMock()
        mock_lf.return_value.flush = MagicMock()

        finalize_trace(mock_trace, {
            "stdout": "",
            "exit_code": 1,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": False,
            "stderr": "error",
        })

        gen_kwargs = mock_trace.generation.call_args[1]
        assert gen_kwargs.get("status_message") == "error"

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_flushes_langfuse(self, mock_lf):
        """Flush must be called to ensure traces are shipped."""
        mock_trace = MagicMock()
        mock_lf.return_value.flush = MagicMock()

        finalize_trace(mock_trace, {
            "stdout": "ok",
            "exit_code": 0,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": True,
            "stderr": "",
        })

        mock_lf.return_value.flush.assert_called_once()

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_graceful_on_generation_exception(self, mock_lf):
        """Exceptions in generation should not propagate."""
        mock_trace = MagicMock()
        mock_trace.generation.side_effect = RuntimeError("span failed")
        mock_lf.return_value.flush = MagicMock()

        # Should not raise
        finalize_trace(mock_trace, {
            "stdout": "ok",
            "exit_code": 0,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": True,
            "stderr": "",
        })

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_metadata_captures_post_diff_stat(self, mock_lf):
        """Post-diff stat line shows what the ribosome changed."""
        mock_trace = MagicMock()
        mock_lf.return_value.flush = MagicMock()

        finalize_trace(mock_trace, {
            "stdout": "ok",
            "exit_code": 0,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": True,
            "stderr": "",
            "post_diff": {"stat": "3 files changed, 10 insertions(+), 2 deletions(-)"},
        })

        meta = mock_trace.generation.call_args[1]["metadata"]
        assert "3 files changed" in meta.get("diff_stat", "")
