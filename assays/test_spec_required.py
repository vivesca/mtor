"""Tests for spec-required dispatch — build mode must have --spec with tests: field.

Validates that:
- Build mode without --spec is rejected
- Build mode with --spec but missing tests: field is rejected
- Build mode with valid spec + existing test files is accepted
- Scout mode without --spec is accepted
- Inline prompt is still required alongside --spec

Runs via: cd ~/code/mtor && uv run pytest assays/test_spec_required.py -v
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _write_spec(tmp_path: Path, frontmatter: str, body: str = "# Spec\n") -> Path:
    """Write a spec file with given frontmatter."""
    spec = tmp_path / "test-spec.md"
    spec.write_text(f"---\n{frontmatter}\n---\n{body}")
    return spec


class TestBuildRequiresSpec:
    """Build mode dispatch must have --spec."""

    def test_build_without_spec_rejected(self, tmp_path):
        """mtor 'prompt' without --spec should be rejected for build mode."""
        from mtor.dispatch import _dispatch_prompt

        # Mock the Temporal client so we don't actually connect
        with patch("mtor.dispatch._get_client") as mock_client, \
             patch("mtor.dispatch._check_worker_sha"):
            # Should exit with NO_SPEC error before reaching Temporal
            try:
                _dispatch_prompt(
                    "Implement foo in ~/code/mtor",
                    mode="build",
                    spec_path=None,
                    skip_sha_check=True,
                )
                # If SystemExit not raised, check that it printed an error
                assert False, "Expected SystemExit for build without --spec"
            except SystemExit as exc:
                # Should be a usage error (exit code 2)
                output = str(exc.code) if exc.code else ""
                assert "NO_SPEC" in output or exc.code == 2 or "spec" in output.lower()

    def test_build_with_spec_accepted(self, tmp_path):
        """mtor --spec valid.md 'prompt' should pass validation."""
        from mtor.dispatch import validate_spec

        # Create a valid spec with tests: field
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        test_file = repo / "assays" / "test_foo.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("def test_something(): pass")

        spec = _write_spec(tmp_path, (
            "status: ready\n"
            f"repo: {repo}\n"
            "files:\n"
            "  - mtor/foo.py\n"
            "tests:\n"
            f"  run: \"cd {repo} && uv run pytest assays/test_foo.py -v\""
        ))

        errors = validate_spec(spec, repo)
        assert errors == [], f"Expected no errors, got: {errors}"


class TestSpecRequiresTestsField:
    """Spec frontmatter must have tests: field for build dispatch."""

    def test_spec_missing_tests_field_rejected(self, tmp_path):
        """Spec without tests: field should fail validation."""
        from mtor.dispatch import validate_spec

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        spec = _write_spec(tmp_path, (
            "status: ready\n"
            f"repo: {repo}\n"
            "files:\n"
            "  - mtor/foo.py"
        ))

        errors = validate_spec(spec, repo)
        assert any("tests" in err.lower() for err in errors), f"Expected tests error, got: {errors}"

    def test_spec_tests_file_missing_rejected(self, tmp_path):
        """Spec with tests: referencing nonexistent file should fail."""
        from mtor.dispatch import validate_spec

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        spec = _write_spec(tmp_path, (
            "status: ready\n"
            f"repo: {repo}\n"
            "files:\n"
            "  - mtor/foo.py\n"
            "tests:\n"
            f"  run: \"cd {repo} && uv run pytest assays/test_nonexistent.py -v\""
        ))

        errors = validate_spec(spec, repo)
        assert any("not found" in err.lower() or "missing" in err.lower() or "exist" in err.lower()
                    for err in errors), f"Expected missing file error, got: {errors}"

    def test_spec_tests_file_exists_accepted(self, tmp_path):
        """Spec with tests: referencing existing file should pass."""
        from mtor.dispatch import validate_spec

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        test_file = repo / "assays" / "test_bar.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("def test_bar(): pass")

        spec = _write_spec(tmp_path, (
            "status: ready\n"
            f"repo: {repo}\n"
            "files:\n"
            "  - mtor/bar.py\n"
            "tests:\n"
            f"  run: \"cd {repo} && uv run pytest assays/test_bar.py -v\""
        ))

        errors = validate_spec(spec, repo)
        assert errors == [], f"Expected no errors, got: {errors}"


class TestScoutNoSpec:
    """Scout mode should work without --spec."""

    def test_scout_without_spec_accepted(self, tmp_path):
        """Scout dispatch without --spec should not be rejected."""
        from mtor.dispatch import _dispatch_prompt

        with patch("mtor.dispatch._get_client") as mock_client, \
             patch("mtor.dispatch._check_worker_sha"):
            # Set up mock to avoid actual Temporal connection
            mock_handle = MagicMock()
            mock_handle.id = "test-workflow-id"
            mock_client.return_value.start_workflow = MagicMock(return_value=mock_handle)

            # Scout mode without --spec should proceed (may fail at Temporal, not at validation)
            try:
                _dispatch_prompt(
                    "Analyze code quality in ~/code/mtor",
                    mode="scout",
                    spec_path=None,
                    skip_sha_check=True,
                )
            except SystemExit as exc:
                output = str(exc.code) if exc.code else ""
                # Should NOT be a NO_SPEC error
                assert "NO_SPEC" not in output, "Scout mode should not require --spec"


class TestInlinePromptStillRequired:
    """Even with --spec, a prompt string is required."""

    def test_inline_prompt_still_required(self, tmp_path):
        """Empty prompt with --spec should still be rejected."""
        from mtor.dispatch import _dispatch_prompt

        spec = _write_spec(tmp_path, "status: ready\nrepo: ~/code/mtor\ntests:\n  run: pytest")

        with patch("mtor.dispatch._get_client"), \
             patch("mtor.dispatch._check_worker_sha"):
            try:
                _dispatch_prompt(
                    "",
                    mode="build",
                    spec_path=spec,
                    skip_sha_check=True,
                )
                assert False, "Expected SystemExit for empty prompt"
            except SystemExit as exc:
                output = str(exc.code) if exc.code else ""
                assert "MISSING_PROMPT" in output or exc.code == 2


class TestSpecStatusValidation:
    """Only ready specs can be dispatched."""

    def test_dispatched_spec_rejected(self, tmp_path):
        """Spec with status: dispatched should fail validation."""
        from mtor.dispatch import validate_spec

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        spec = _write_spec(tmp_path, (
            "status: dispatched\n"
            f"repo: {repo}\n"
            "files:\n"
            "  - mtor/foo.py\n"
            "tests:\n"
            f"  run: \"cd {repo} && uv run pytest assays/test_foo.py -v\""
        ))

        errors = validate_spec(spec, repo)
        assert any("status" in err.lower() for err in errors), f"Expected status error, got: {errors}"

    def test_done_spec_rejected(self, tmp_path):
        """Spec with status: done should fail validation."""
        from mtor.dispatch import validate_spec

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        spec = _write_spec(tmp_path, (
            "status: done\n"
            f"repo: {repo}\n"
            "files:\n"
            "  - mtor/foo.py\n"
            "tests:\n"
            f"  run: \"cd {repo} && uv run pytest assays/test_foo.py -v\""
        ))

        errors = validate_spec(spec, repo)
        assert any("status" in err.lower() for err in errors)
