"""Chaperone review activity for ribosome results."""

from __future__ import annotations

import json
import re as _re
import time as _time
from pathlib import Path

from temporalio import activity

REVIEW_LOG = Path.home() / "germline" / "loci" / "ribosome-reviews.jsonl"

_DESTRUCTION_PATTERNS = _re.compile(
    r"rm -rf|rmdir|replaced entire|overwrote|deleted all|"
    r"file is now empty|wrote 0 bytes|No such file",
    _re.IGNORECASE,
)

_ERROR_PATTERNS = _re.compile(
    r"SyntaxError|ImportError|ModuleNotFoundError|PermissionError|"
    r"Traceback \(most recent|panic:|fatal:",
    _re.IGNORECASE,
)



def _is_blocking_review_flag(flag: str) -> bool:
    """Return True for flags unsafe enough to block an automatic merge."""
    if flag == "no_commit_on_success":
        return True
    return flag.startswith(
        (
            "destruction",
            "errors",
            "file_shrunk",
            "pure_deletion",
            "target_file_missing",
            "py2_except_syntax",
            "nested_test_file",
        )
    )


_PLACEHOLDER_PATTERNS = _re.compile(r"\bTODO\b|\bFIXME\b|\bstub\b", _re.IGNORECASE)
_HARDCODED_HOME = _re.compile(r"/Users/terry/|/home/terry/")
_PY2_EXCEPT = _re.compile(r"^\s*except\s+\w+\s*,\s*\w+\s*:", _re.MULTILINE)
_DUPE_FUTURE = _re.compile(r"from\s+__future__\s+import\s+annotations")



@activity.defn
async def chaperone(result: dict) -> dict:
    """Review a ribosome task result for quality signals.

    Returns {"approved": bool, "flags": [...], "verdict": str}.
    """
    task = result.get("task", "")
    provider = result.get("provider", "")
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    exit_code = result.get("exit_code", -1)
    combined = f"{stdout}\n{stderr}"

    flags: list[str] = []

    if exit_code != 0:
        flags.append(f"exit_code={exit_code}")

    destruction_hits = _DESTRUCTION_PATTERNS.findall(combined)
    if destruction_hits:
        flags.append(f"destruction: {', '.join(list(set(destruction_hits))[:3])}")

    error_hits = _ERROR_PATTERNS.findall(combined)
    if error_hits:
        flags.append(f"errors: {', '.join(list(set(error_hits))[:3])}")

    # Coaching-promoted checks (deterministic, formerly prose-only)
    placeholder_hits = _PLACEHOLDER_PATTERNS.findall(combined)
    if placeholder_hits:
        flags.append(f"placeholders: {', '.join(list(set(placeholder_hits))[:3])}")

    if _HARDCODED_HOME.search(combined):
        flags.append("hardcoded_home_path")

    if _PY2_EXCEPT.search(combined):
        flags.append("py2_except_syntax")

    # Check for duplicate `from __future__ import annotations` per file in output
    future_count = len(_DUPE_FUTURE.findall(combined))
    if future_count > 1:
        flags.append(f"dupe_future_import: {future_count} occurrences")

    task_words = len(task.split())
    output_words = len(stdout.split())
    if task_words > 20 and output_words < 10 and exit_code == 0:
        flags.append(f"thin_output: {output_words} words for {task_words}-word task")

    if exit_code == 0 and len(stdout.strip()) < 5:
        flags.append("empty_stdout_on_success")

    # GLM ran to completion but committed nothing -- likely no-op
    post_stat_text = (
        result.get("post_diff", {}).get("stat", "")
        if isinstance(result.get("post_diff"), dict)
        else ""
    )
    commit_count = (
        result.get("post_diff", {}).get("commit_count", 0)
        if isinstance(result.get("post_diff"), dict)
        else 0
    )
    branch_name = result.get("branch_name", "")

    # Test files must be in assays/ flat (not nested subdirectories)
    if post_stat_text:
        for line in post_stat_text.splitlines():
            fname = line.strip().split("|")[0].strip() if "|" in line else line.strip()
            if fname.startswith("assays/") and fname.count("/") > 1 and "test_" in fname:
                flags.append(f"nested_test_file: {fname}")

    # Primary diff evidence: commits list, patch content, or stat — any of these
    # means real work was done, even if one derivative signal is empty/truncated.
    post_patch = (
        result.get("post_diff", {}).get("patch", "")
        if isinstance(result.get("post_diff"), dict)
        else ""
    )
    commits_list = (
        result.get("post_diff", {}).get("commits", [])
        if isinstance(result.get("post_diff"), dict)
        else []
    )
    has_primary_evidence = (
        bool(post_stat_text.strip())
        or commit_count > 0
        or bool(commits_list)
        or bool(post_patch.strip())
    )
    if exit_code == 0 and not has_primary_evidence:
        flags.append("no_commit_on_success")

    # Extract ALL file paths mentioned in the task and check if they appear in the diff.
    # Catches "task mentions dispatch.py but diff only touches cli.py" mismatches.
    task_files = set(_re.findall(r"[\w./~-]+\.(?:py|sh|toml|md|yaml|yml|json)", task))
    # Filter out common false positives (URLs, example paths in code blocks)
    task_files = {f for f in task_files if not f.startswith("http") and len(f) > 4}
    if task_files and exit_code == 0 and post_stat_text:
        # Normalize: strip ~/ and leading path prefixes to match git diff short paths
        def _normalize(path: str) -> str:
            path = path.lstrip("~/")
            # Strip common prefixes that git diff won't show
            for prefix in ("germline/", "home/vivesca/germline/"):
                path = path.removeprefix(prefix)
            return path

        diff_files = set()
        for line in post_stat_text.splitlines():
            fname = line.strip().split("|")[0].strip() if "|" in line else ""
            if fname:
                diff_files.add(fname)

        for task_file in task_files:
            norm = _normalize(task_file)
            if (
                norm
                and not any(norm in df or df.endswith(norm) for df in diff_files)
                and any(
                    kw in task.lower()
                    for kw in ["modify", "edit", "change", "add to", "update", "fix", "create"]
                )
            ):
                flags.append(f"target_file_missing: {norm}")

    pre_diff = result.get("pre_diff", {})
    post_diff = result.get("post_diff", {})
    pre_numstat = pre_diff.get("numstat", "") if isinstance(pre_diff, dict) else ""
    post_numstat = post_diff.get("numstat", "") if isinstance(post_diff, dict) else ""
    post_stat = post_diff.get("stat", "") if isinstance(post_diff, dict) else str(post_diff)

    if post_numstat and post_numstat != pre_numstat:
        # Compute overall net additions from numstat to detect false-positive
        # per-file shrink/deletion flags caused by truncated diff text.
        total_added = 0
        total_removed = 0
        for line in post_numstat.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                try:
                    total_added += int(parts[0])
                    total_removed += int(parts[1])
                except ValueError:
                    pass
        net_positive = total_added > total_removed

        for line in post_numstat.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                added, removed, fname = parts
                try:
                    a, r = int(added), int(removed)
                    if r > a * 3 and r > 10:
                        if not net_positive:
                            flags.append(f"file_shrunk: {fname} +{a}/-{r}")
                    if a == 0 and r > 5:
                        if not net_positive:
                            flags.append(f"pure_deletion: {fname} -{r}")
                except ValueError:
                    pass

    # Determine verdict: incomplete when work was done but process failed
    if exit_code != 0 and commit_count > 0:
        verdict = "incomplete"
        approved = False
    else:
        result_mode = result.get("mode", "build")
        if result_mode == "scout":
            # Scout tasks don't produce commits — different approval criteria
            approved = exit_code == 0 and not any(f.startswith("destruction") for f in flags)
            # Don't flag no_commit_on_success or empty_stdout_on_success for scout
            flags = [
                f for f in flags if f not in ("no_commit_on_success", "empty_stdout_on_success")
            ]
        else:
            approved = exit_code == 0 and not any(
                _is_blocking_review_flag(f) for f in flags
            )
        verdict = "approved" if approved else "rejected"
        if flags and approved:
            verdict = "approved_with_flags"

    review = {
        "ts": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        "task": task[:200],
        "provider": provider,
        "exit_code": exit_code,
        "flags": flags,
        "verdict": verdict,
        "stdout_len": len(stdout),
        "stderr_len": len(stderr),
        "diff": post_stat[:500] if post_stat else "",
        "cost_info": result.get("cost_info", ""),
    }
    if verdict == "incomplete" and branch_name:
        review["branch_name"] = branch_name

    try:
        REVIEW_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(REVIEW_LOG, "a") as f:
            f.write(json.dumps(review) + "\n")
    except OSError:
        pass

    # Satisfaction scoring: 0-100 based on objective signals
    score = 100

    # Major deductions
    if exit_code != 0:
        score -= 40
    if not post_stat_text.strip():
        score -= 30  # no changes
    if any(f.startswith("destruction") for f in flags):
        score -= 50

    # Moderate deductions
    if any("file_shrunk" in f for f in flags):
        score -= 20
    if any("thin_output" in f for f in flags):
        score -= 15
    if any("placeholders" in f for f in flags):
        score -= 10
    if any("errors" in f for f in flags):
        score -= 10

    # Bonuses
    if commit_count > 0 and exit_code == 0:
        score += 10  # actually committed
    if any("test" in f.lower() for f in post_stat_text.splitlines()):
        score += 5  # includes test files

    # Fallback diff bonus: main..HEAD was empty but base_sha..HEAD captured work
    if isinstance(post_diff, dict) and post_diff.get("fallback") and post_stat_text.strip():
        score += 10

    score = max(0, min(100, score))

    requeue_prompt = ""
    if verdict in ("rejected", "incomplete") and any("thin_output" in f for f in flags):
        requeue_prompt = task[:200] + " -- Be thorough. Read files before editing. Show your work."
    elif verdict in ("rejected", "incomplete") and any("file_shrunk" in f for f in flags):
        requeue_prompt = (
            task[:200]
            + " -- IMPORTANT: Read the entire file before modifying. Preserve ALL existing content."
        )

    return {
        "approved": approved,
        "flags": flags,
        "verdict": verdict,
        "satisfaction": score,
        "requeue_prompt": requeue_prompt,
    }
