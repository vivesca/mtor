"""Tests for Claude Code stream-json normalization in translocase.

Claude's harness (``--output-format stream-json --verbose``) emits a
multi-megabyte newline-delimited JSON event stream. Until this helper
landed, ``translate()`` shipped only ``stdout[:1000]`` to chaperone --
which on a real session is just one minified ``type: "system",
subtype: "init"`` record. The helper projects the full session into a
concise, bounded review payload (verification evidence first, then the
final report) so chaperone sees what actually happened.

Runs via: cd ~/code/mtor && uv run pytest assays/test_claude_output_normalization.py -v
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtor.worker.translocase import (
    _CLAUDE_RESULT_BUDGET,
    _CLAUDE_TOTAL_BUDGET,
    _CLAUDE_VERIFY_CMD_BUDGET,
    _CLAUDE_VERIFY_MAX_ITEMS,
    _CLAUDE_VERIFY_RESULT_BUDGET,
    _extract_claude_final_report,
    _extract_claude_verification_evidence,
    _normalize_claude_stream_json,
    _parse_claude_stream_records,
)


# ---------------------------------------------------------------------------
# Compact realistic stream-json builders
# ---------------------------------------------------------------------------


def _record(*, type: str, **fields) -> str:
    """Serialize one stream-json record to a single line."""
    return json.dumps({"type": type, **fields})


def _assistant_tool_use(
    call_id: str, command: str, *, name: str = "Bash", description: str = ""
) -> str:
    """A ``type:assistant`` record carrying one tool_use entry."""
    input_obj = {"command": command}
    if description:
        input_obj["description"] = description
    return _record(
        type="assistant",
        message={
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": call_id, "name": name, "input": input_obj}
            ],
        },
    )


def _tool_result(call_id: str, content: str, *, is_error: bool = False) -> str:
    """A ``type:user`` record carrying the matching tool_result entry."""
    return _record(
        type="user",
        message={
            "role": "user",
            "content": [
                {
                    "tool_use_id": call_id,
                    "type": "tool_result",
                    "content": content,
                    "is_error": is_error,
                }
            ],
        },
    )


def _result_record(text: str, *, subtype: str = "success") -> str:
    return _record(type="result", subtype=subtype, is_error=False, result=text)


def _full_session(
    *, result_text: str = "## Report: DONE\n\nImplemented and verified."
) -> str:
    """Compact stream-json session: init -> Bash pytest -> result -> final."""
    return "\n".join(
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "cwd": "/home/user/code/repo",
                    "session_id": "abc",
                    "tools": ["Bash", "Read", "Edit"],
                }
            ),
            _assistant_tool_use(
                "call_1",
                "cd ~/code/repo && uv run pytest assays/test_foo.py -q 2>&1 | tail -5",
                description="Run focused suite",
            ),
            _tool_result(
                "call_1",
                "..........                                                               [100%]\n10 passed in 0.93s",
            ),
            _result_record(result_text),
        ]
    )


# ---------------------------------------------------------------------------
# Record parsing
# ---------------------------------------------------------------------------


class TestParseClaudeStreamRecords:
    """_parse_claude_stream_records tolerates non-JSON lines defensively."""

    def test_parses_ndjson_records(self):
        records = _parse_claude_stream_records(
            json.dumps({"type": "system", "subtype": "init"})
        )
        assert len(records) == 1
        assert records[0]["type"] == "system"

    def test_skips_blank_and_non_json_lines(self):
        stdout = "\n".join(
            [
                "not json",
                "",
                json.dumps({"type": "system"}),
                "{ malformed",  # starts with { but doesn't parse
                json.dumps({"type": "result"}),
            ]
        )
        records = _parse_claude_stream_records(stdout)
        assert [r["type"] for r in records] == ["system", "result"]

    def test_returns_empty_for_empty_input(self):
        assert _parse_claude_stream_records("") == []

    def test_ignores_non_object_json(self):
        records = _parse_claude_stream_records('42\ntrue\n"hi"\nnull')
        assert records == []


# ---------------------------------------------------------------------------
# Final report extraction
# ---------------------------------------------------------------------------


class TestExtractClaudeFinalReport:
    """_extract_claude_final_report returns the latest successful result text."""

    def test_returns_success_result_text(self):
        records = [
            {"type": "system", "subtype": "init"},
            {"type": "result", "subtype": "success", "result": "Done."},
        ]
        assert _extract_claude_final_report(records) == "Done."

    def test_returns_latest_when_multiple_success(self):
        records = [
            {"type": "result", "subtype": "success", "result": "first"},
            {"type": "result", "subtype": "success", "result": "second"},
        ]
        assert _extract_claude_final_report(records) == "second"

    def test_ignores_error_subtypes(self):
        records = [
            {"type": "result", "subtype": "error_max_tokens", "result": "partial"},
        ]
        assert _extract_claude_final_report(records) is None

    def test_ignores_empty_result_string(self):
        records = [{"type": "result", "subtype": "success", "result": "   "}]
        assert _extract_claude_final_report(records) is None

    def test_returns_none_when_no_result_record(self):
        records = [{"type": "system"}, {"type": "assistant"}]
        assert _extract_claude_final_report(records) is None


# ---------------------------------------------------------------------------
# Verification evidence extraction
# ---------------------------------------------------------------------------


class TestExtractClaudeVerificationEvidence:
    """_extract_claude_verification_evidence pairs tool_use with tool_result."""

    def test_pairs_pytest_command_with_passing_result(self):
        records = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "Bash",
                            "input": {"command": "uv run pytest assays/test_foo.py -q"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "content": "10 passed in 0.93s",
                            "is_error": False,
                        }
                    ]
                },
            },
        ]
        evidence = _extract_claude_verification_evidence(records)
        assert len(evidence) == 1
        cmd, tail = evidence[0]
        assert cmd == "uv run pytest assays/test_foo.py -q"
        assert "10 passed" in tail

    def test_strips_cd_preamble_from_command(self):
        records = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_x",
                            "name": "Bash",
                            "input": {
                                "command": "cd ~/code/repo && cd subdir && uv run pytest -q"
                            },
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_x",
                            "content": "all passed",
                        }
                    ]
                },
            },
        ]
        evidence = _extract_claude_verification_evidence(records)
        assert evidence[0][0] == "uv run pytest -q"

    def test_recognizes_each_spec_verifier(self):
        """pytest, ruff, mypy, npm test, cargo test, go test are all detected."""
        commands = [
            "pytest",
            "uv run pytest",
            "ruff check .",
            "uv run ruff check .",
            "mypy mtor/",
            "uv run mypy mtor/",
            "npm test",
            "npm run test",
            "pnpm test",
            "yarn test",
            "bun test",
            "cargo test",
            "go test ./...",
        ]
        for i, cmd in enumerate(commands):
            records = [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"c{i}",
                                "name": "Bash",
                                "input": {"command": cmd},
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"c{i}",
                                "content": "ok",
                            }
                        ]
                    },
                },
            ]
            evidence = _extract_claude_verification_evidence(records)
            assert len(evidence) == 1, f"missed verifier: {cmd!r}"

    def test_failed_test_result_preserved_as_failed(self):
        records = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "c1",
                            "name": "Bash",
                            "input": {"command": "uv run pytest assays/test_bad.py"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "c1",
                            "content": "FAILED assays/test_bad.py::test_x\n1 failed in 0.5s",
                            "is_error": True,
                        }
                    ]
                },
            },
        ]
        evidence = _extract_claude_verification_evidence(records)
        assert len(evidence) == 1
        tail = evidence[0][1]
        assert "1 failed" in tail
        assert "FAILED" in tail

    def test_skips_arbitrary_and_destructive_commands(self):
        """Neither ``rm -rf`` nor ``git push`` is a test/quality command."""
        commands = [
            "rm -rf /tmp/scratch",
            "git push --force origin main",
            "sudo apt install -y something",
            "dd if=/dev/zero of=/disk",
            "grep -m1 'pytest passed' worker.log",
            "rg 'uv run pytest' logs/",
            "echo pytest",
        ]
        for i, cmd in enumerate(commands):
            records = [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"c{i}",
                                "name": "Bash",
                                "input": {"command": cmd},
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"c{i}",
                                "content": "done",
                            }
                        ]
                    },
                },
            ]
            assert _extract_claude_verification_evidence(records) == [], (
                f"should not surface arbitrary command: {cmd!r}"
            )

    def test_preserves_invocation_order(self):
        records = []
        for i, cmd in enumerate(["pytest a", "pytest b", "pytest c"]):
            records.extend(
                [
                    json.loads(_assistant_tool_use(f"c{i}", cmd)),
                    json.loads(_tool_result(f"c{i}", f"result-{i}")),
                ]
            )

        evidence = _extract_claude_verification_evidence(records)

        assert [cmd for cmd, _ in evidence] == ["pytest a", "pytest b", "pytest c"]

    def test_bounded_window_prefers_latest_verifiers(self):
        records = []
        total = _CLAUDE_VERIFY_MAX_ITEMS + 2
        for i in range(total):
            records.extend(
                [
                    json.loads(_assistant_tool_use(f"c{i}", f"pytest test_{i}.py")),
                    json.loads(_tool_result(f"c{i}", f"result-{i}")),
                ]
            )

        evidence = _extract_claude_verification_evidence(records)

        assert len(evidence) == _CLAUDE_VERIFY_MAX_ITEMS
        assert evidence[0][0] == "pytest test_2.py"
        assert evidence[-1][0] == f"pytest test_{total - 1}.py"

    def test_caps_items_at_max(self):
        records = []
        for i in range(_CLAUDE_VERIFY_MAX_ITEMS + 3):
            records.append(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"c{i}",
                                "name": "Bash",
                                "input": {"command": "pytest"},
                            }
                        ]
                    },
                }
            )
            records.append(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"c{i}",
                                "content": "ok",
                            }
                        ]
                    },
                }
            )
        evidence = _extract_claude_verification_evidence(records)
        assert len(evidence) == _CLAUDE_VERIFY_MAX_ITEMS

    def test_orphan_tool_result_without_tool_use_ignored(self):
        records = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "orphan",
                            "content": "pytest output",
                        }
                    ]
                },
            },
        ]
        assert _extract_claude_verification_evidence(records) == []


# ---------------------------------------------------------------------------
# End-to-end normalization
# ---------------------------------------------------------------------------


class TestNormalizeClaudeStreamJson:
    """_normalize_claude_stream_json returns a bounded, ordered review payload."""

    def test_final_report_extraction_from_successful_result(self):
        stdout = _full_session(result_text="## Report: DONE\n\nShipped.")
        normalized = _normalize_claude_stream_json(stdout)
        assert "## Report: DONE" in normalized
        assert "Shipped." in normalized

    def test_pytest_command_and_passing_result_extracted(self):
        stdout = _full_session()
        normalized = _normalize_claude_stream_json(stdout)
        assert "uv run pytest assays/test_foo.py -q" in normalized
        assert "10 passed" in normalized

    def test_evidence_precedes_final_report(self):
        stdout = _full_session()
        normalized = _normalize_claude_stream_json(stdout)
        verify_pos = normalized.find("[auto-verify]")
        report_pos = normalized.find("## Report: DONE")
        assert verify_pos != -1, "no [auto-verify] block in payload"
        assert report_pos != -1, "no final report in payload"
        assert verify_pos < report_pos

    def test_normalized_payload_respects_total_bound(self):
        huge_report = "X" * (_CLAUDE_RESULT_BUDGET * 5)
        records = []
        for i in range(_CLAUDE_VERIFY_MAX_ITEMS + 5):
            records.append(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"c{i}",
                                "name": "Bash",
                                "input": {"command": "pytest"},
                            }
                        ]
                    },
                }
            )
            records.append(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"c{i}",
                                "content": "Y" * (_CLAUDE_VERIFY_RESULT_BUDGET * 3),
                            }
                        ]
                    },
                }
            )
        records.append({"type": "result", "subtype": "success", "result": huge_report})
        stdout = "\n".join(json.dumps(r) for r in records)
        normalized = _normalize_claude_stream_json(stdout)
        assert len(normalized) <= _CLAUDE_TOTAL_BUDGET

    def test_each_command_truncated_to_cmd_bound(self):
        long_cmd = "pytest " + "x" * (_CLAUDE_VERIFY_CMD_BUDGET * 3)
        records = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "c1",
                            "name": "Bash",
                            "input": {"command": long_cmd},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "c1", "content": "ok"}
                    ]
                },
            },
            {"type": "result", "subtype": "success", "result": "Done"},
        ]
        stdout = "\n".join(json.dumps(r) for r in records)
        normalized = _normalize_claude_stream_json(stdout)
        auto_verify_lines = [
            line for line in normalized.splitlines() if line.startswith("[auto-verify]")
        ]
        assert auto_verify_lines, "expected at least one [auto-verify] line"
        for line in auto_verify_lines:
            cmd_text = line[len("[auto-verify] ") :]
            assert len(cmd_text) <= _CLAUDE_VERIFY_CMD_BUDGET

    def test_each_result_tail_truncated_to_result_bound(self):
        long_tail = "z\n" * (_CLAUDE_VERIFY_RESULT_BUDGET * 2)
        records = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "c1",
                            "name": "Bash",
                            "input": {"command": "pytest"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "c1",
                            "content": long_tail,
                        }
                    ]
                },
            },
            {"type": "result", "subtype": "success", "result": "Done"},
        ]
        stdout = "\n".join(json.dumps(r) for r in records)
        normalized = _normalize_claude_stream_json(stdout)
        # The tail block sits between the [auto-verify] line and the next
        # blank-line separator.
        verify_start = normalized.find("[auto-verify] pytest")
        assert verify_start != -1
        next_blank = normalized.find("\n\n", verify_start)
        tail_section = normalized[verify_start:next_blank]
        tail_lines = tail_section.splitlines()[1:]
        tail_text = "\n".join(tail_lines)
        assert len(tail_text) <= _CLAUDE_VERIFY_RESULT_BUDGET

    def test_malformed_lines_mixed_with_valid_are_skipped(self):
        stdout = "\n".join(
            [
                "not json at all",
                json.dumps({"type": "system"}),
                "{ broken",
                _assistant_tool_use("c1", "uv run pytest -q"),
                "random prose",
                _tool_result("c1", "5 passed"),
                _result_record("Done."),
            ]
        )
        normalized = _normalize_claude_stream_json(stdout)
        assert "5 passed" in normalized
        assert "Done." in normalized
        # Non-JSON noise must NOT leak through.
        assert "not json at all" not in normalized
        assert "random prose" not in normalized

    def test_absent_result_record_falls_back_to_truncated_stdout(self):
        stdout = "{" + "x" * 200 + "}"  # bogus record, no result
        normalized = _normalize_claude_stream_json(stdout)
        assert normalized == stdout[:1000]

    def test_empty_result_record_falls_back_to_truncated_stdout(self):
        stdout = json.dumps({"type": "result", "subtype": "success", "result": ""})
        normalized = _normalize_claude_stream_json(stdout)
        assert normalized == stdout[:1000]

    def test_error_subtype_falls_back_to_truncated_stdout(self):
        stdout = json.dumps(
            {"type": "result", "subtype": "error_max_tokens", "result": "partial"}
        )
        normalized = _normalize_claude_stream_json(stdout)
        assert normalized == stdout[:1000]

    def test_empty_stdout_returned_unchanged(self):
        assert _normalize_claude_stream_json("") == ""

    def test_destructive_command_not_leaked_into_payload(self):
        """A destructive Bash invocation must never reach the review payload."""
        records = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "c1",
                            "name": "Bash",
                            "input": {"command": "rm -rf / && pytest"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "c1",
                            "content": "oops",
                        }
                    ]
                },
            },
            {"type": "result", "subtype": "success", "result": "Done"},
        ]
        stdout = "\n".join(json.dumps(r) for r in records)
        normalized = _normalize_claude_stream_json(stdout)
        assert "rm -rf" not in normalized
        assert "oops" not in normalized
        # The final report still surfaces.
        assert "Done" in normalized

    def test_failed_test_evidence_visibly_failed(self):
        """End-to-end: a failed pytest result must read as failed."""
        stdout = "\n".join(
            [
                _assistant_tool_use("c1", "uv run pytest assays/test_bad.py"),
                _tool_result(
                    "c1",
                    "FAILED assays/test_bad.py::test_x\n1 failed in 0.5s",
                    is_error=True,
                ),
                _result_record("Done with concerns."),
            ]
        )
        normalized = _normalize_claude_stream_json(stdout)
        assert "1 failed" in normalized
        assert "FAILED" in normalized
        assert "Done with concerns." in normalized

    def test_arbitrary_command_not_copied_to_payload(self):
        """git push output is not test evidence -- must not be projected."""
        stdout = "\n".join(
            [
                _assistant_tool_use("c1", "git push --force origin main"),
                _tool_result("c1", "pushed"),
                _result_record("Done."),
            ]
        )
        normalized = _normalize_claude_stream_json(stdout)
        assert "git push --force" not in normalized
        assert "pushed" not in normalized
        assert "Done." in normalized

    def test_exception_during_parse_falls_back_to_truncated_stdout(self, monkeypatch):
        """If parsing raises, the helper returns the existing payload unchanged."""

        def boom(_stdout: str):
            raise RuntimeError("parse blew up")

        monkeypatch.setattr(
            "mtor.worker.translocase._parse_claude_stream_records", boom
        )
        stdout = _full_session()
        normalized = _normalize_claude_stream_json(stdout)
        assert normalized == stdout[:1000]


# ---------------------------------------------------------------------------
# Integration: translate() plumbs the helper into both success paths
# ---------------------------------------------------------------------------


def _build_subprocess_mock(stdout_bytes: bytes) -> AsyncMock:
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.pid = 12345
    mock_proc.stdout = AsyncMock()
    mock_proc.stderr = AsyncMock()
    mock_proc.stdout.read = AsyncMock(side_effect=[stdout_bytes, b""])
    mock_proc.stderr.read = AsyncMock(side_effect=[b"", b""])
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.terminate = MagicMock()
    mock_proc.kill = MagicMock()
    return mock_proc


def _patch_translate_seam(tmp_path, *, select_provider_return: str):
    """Yield patches sufficient to drive translate() to a return path.

    Also redirects OUTPUT_DIR to *tmp_path* so the persistent ribosome-outputs
    cache never short-circuits a later integration test (the cache key is the
    date + ``[t-XXX]`` task id, and translate returns a cached result before
    the subprocess runs).
    """
    from contextlib import contextmanager

    @contextmanager
    def _seam(mock_run, mock_proc, workflow_id: str):
        mock_info = MagicMock()
        mock_info.workflow_id = workflow_id
        with (
            patch("mtor.worker.translocase._subprocess.run", side_effect=mock_run),
            patch(
                "mtor.worker.translocase.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch("mtor.worker.translocase.load_health", return_value={}),
            patch(
                "mtor.worker.translocase.select_provider",
                return_value=select_provider_return,
            ),
            patch(
                "mtor.worker.translocase.persist_health_result", return_value={}
            ),
            patch(
                "mtor.worker.translocase.parse_rate_limit_window",
                return_value=None,
            ),
            patch("mtor.worker.translocase.activity.info", return_value=mock_info),
            patch("mtor.worker.translocase.activity.heartbeat"),
            patch(
                "mtor.worker.translocase.activity.is_cancelled",
                return_value=False,
            ),
            patch("mtor.worker.translocase.create_task_trace", return_value=None),
            patch("mtor.worker.translocase.finalize_trace"),
            patch(
                "mtor.worker.translocase._create_worktree",
                return_value=str(tmp_path / "worktree"),
            ),
            patch("mtor.worker.translocase._detect_prior_commits", return_value=[]),
            patch("mtor.worker.translocase._git_pull_ff_only"),
            patch("mtor.worker.translocase._remove_worktree_if_inactive"),
            patch("mtor.worker.translocase.OUTPUT_DIR", tmp_path / "ribosome-outputs"),
        ):
            yield

    return _seam


class TestTranslateUsesNormalizeHelper:
    """Smallest integration assertions: translate() uses the helper on the
    early-exit-clean and normal return paths when ``harness == 'claude'``."""

    def test_normal_return_path_uses_normalized_stdout(self, tmp_path):
        """translate() with harness='claude' routes the helper output into
        ``result['stdout']`` on the normal return path."""
        from mtor.worker.translocase import translate

        final_text = "ClaudeFinalReportSentinel-normal-path"
        stream = _full_session(result_text=final_text)
        # Distinct hex task id per test so the OUTPUT_DIR cache never
        # short-circuits the subprocess path (cache key is date + tid).
        task = "[t-dead001] Test claude normalize normal path"

        def mock_run(cmd, **kwargs):
            if cmd[0] == "pgrep":
                return subprocess.CompletedProcess(
                    cmd, returncode=0, stdout="0\n", stderr=""
                )
            if cmd[0] == "bash" and "-n" in cmd:
                return subprocess.CompletedProcess(
                    cmd, returncode=0, stdout="", stderr=""
                )
            if cmd[0] == "git":
                return subprocess.CompletedProcess(
                    cmd, returncode=0, stdout="aaa111\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        mock_proc = _build_subprocess_mock(stream.encode())
        seam = _patch_translate_seam(tmp_path, select_provider_return="claude")

        with seam(mock_run, mock_proc, "test-claude-normal-return"):
            result = asyncio.run(
                translate(
                    task=task,
                    provider="claude",
                    mode="build",
                    repo=str(tmp_path / "repo"),
                    harness="claude",
                )
            )

        # The final-report sentinel proves the helper was invoked and its
        # output landed in the chaperone-facing stdout.
        assert final_text in result["stdout"]
        # The pytest verification evidence is also projected.
        assert "uv run pytest assays/test_foo.py -q" in result["stdout"]
        assert "10 passed" in result["stdout"]
        # Bounded total payload (helper cap).
        assert len(result["stdout"]) <= _CLAUDE_TOTAL_BUDGET

    def test_non_claude_harness_passes_through_truncated_stdout(self, tmp_path):
        """Non-claude harness skips normalization; stdout stays at [:1000]."""
        from mtor.worker.translocase import translate

        final_text = "ClaudeFinalReportSentinel-passthrough"
        stream = _full_session(result_text=final_text)
        task = "[t-dead002] Test opencode passthrough"

        def mock_run(cmd, **kwargs):
            if cmd[0] == "pgrep":
                return subprocess.CompletedProcess(
                    cmd, returncode=0, stdout="0\n", stderr=""
                )
            if cmd[0] == "bash" and "-n" in cmd:
                return subprocess.CompletedProcess(
                    cmd, returncode=0, stdout="", stderr=""
                )
            if cmd[0] == "git":
                return subprocess.CompletedProcess(
                    cmd, returncode=0, stdout="aaa111\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        mock_proc = _build_subprocess_mock(stream.encode())
        seam = _patch_translate_seam(tmp_path, select_provider_return="zhipu")

        with seam(mock_run, mock_proc, "test-claude-passthrough"):
            result = asyncio.run(
                translate(
                    task=task,
                    provider="zhipu",
                    mode="build",
                    repo=str(tmp_path / "repo"),
                    harness="opencode",
                )
            )

        # Non-claude path keeps the existing truncation. The helper's
        # sentinel (which only appears inside the stream-json final record)
        # proves normalization was skipped.
        assert result["stdout"] == stream[:1000]

    def test_early_exit_clean_path_uses_normalized_stdout(self, tmp_path):
        """translate() with harness='claude' + test_paths routes the helper
        output through the early-exit-clean path; the translocase-run
        ``[auto-verify]`` block is appended after the normalized payload."""
        from mtor.worker.translocase import translate

        final_text = "ClaudeFinalReportSentinel-early-exit"
        stream = _full_session(result_text=final_text)
        rev_parse_count = 0

        def mock_run(cmd, **kwargs):
            nonlocal rev_parse_count
            if cmd[0] == "pgrep":
                return subprocess.CompletedProcess(
                    cmd, returncode=0, stdout="0\n", stderr=""
                )
            if cmd[0] == "bash" and "-n" in cmd:
                return subprocess.CompletedProcess(
                    cmd, returncode=0, stdout="", stderr=""
                )
            if cmd[0] == "uv" and "pytest" in cmd:
                return subprocess.CompletedProcess(
                    cmd, returncode=0, stdout="1 passed\n", stderr=""
                )
            if cmd[0] == "git":
                subcmd = cmd[1] if len(cmd) > 1 else ""
                if subcmd == "rev-parse":
                    rev_parse_count += 1
                    sha = "aaa111\n" if rev_parse_count == 1 else "bbb222\n"
                    return subprocess.CompletedProcess(
                        cmd, returncode=0, stdout=sha, stderr=""
                    )
                if subcmd == "rev-list":
                    return subprocess.CompletedProcess(
                        cmd, returncode=0, stdout="1\n", stderr=""
                    )
                return subprocess.CompletedProcess(
                    cmd, returncode=0, stdout="", stderr=""
                )
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        mock_proc = _build_subprocess_mock(stream.encode())
        seam = _patch_translate_seam(tmp_path, select_provider_return="claude")

        task = (
            "---\ntests:\n  - assays/test_zombie_early_exit.py\n---\n"
            "[t-dead003] Fix bug"
        )

        with seam(mock_run, mock_proc, "test-claude-early-exit"):
            result = asyncio.run(
                translate(
                    task=task,
                    provider="claude",
                    mode="build",
                    repo=str(tmp_path / "repo"),
                    harness="claude",
                )
            )

        assert result.get("verdict") == "early_exit_clean"
        # The normalized stream payload is present.
        assert final_text in result["stdout"]
        assert "uv run pytest assays/test_foo.py -q" in result["stdout"]
        # The translocase-run verifier block is also present, appended
        # AFTER the normalized payload.
        assert "uv run pytest -x assays/test_zombie_early_exit.py" in result["stdout"]
        # Both pytest results are visible.
        assert "10 passed" in result["stdout"]  # from the stream
        assert "1 passed" in result["stdout"]  # from the translocase verifier
        # The stream-final sentinel appears before the translocase verifier
        # block (verifier is appended at the end).
        sentinel_pos = result["stdout"].find(final_text)
        verifier_pos = result["stdout"].find("[auto-verify] uv run pytest -x")
        assert sentinel_pos != -1
        assert verifier_pos != -1
        assert sentinel_pos < verifier_pos
