"""Chaperone review activity for ribosome results."""

from __future__ import annotations

import json
import os
import re as _re
import time as _time
from pathlib import Path

from temporalio import activity

# Path overridable via env so tests (or alternate hosts) never append to the
# real production ledger. Tests redirect this per-call; the env fallback is a
# belt-and-suspenders guard for any path that bypasses the monkeypatch.
_REVIEW_LOG_ENV = os.environ.get("MTOR_REVIEW_LOG")
REVIEW_LOG = (
    Path(_REVIEW_LOG_ENV)
    if _REVIEW_LOG_ENV
    else Path.home() / "germline" / "loci" / "ribosome-reviews.jsonl"
)
DOSSIER_DIR = Path.home() / "germline" / "loci" / "ribosome-dossiers"

# Narration patterns that report destruction *happened*. Bare "No such file"
# was deliberately removed (review-gate-1): it signals a file was ABSENT
# (benign FileNotFoundError / `ls`-style narration), not that anything was
# destroyed, yet it tripped the unconditional destruction hard-reject below
# and sank good committed work. Genuine destructive code committed to the diff
# is caught by _DESTRUCTION_SHELL_PATTERNS against added patch lines.
_DESTRUCTION_PATTERNS = _re.compile(
    r"rm -rf|rmdir|replaced entire|overwrote|deleted all|"
    r"file is now empty|wrote 0 bytes",
    _re.IGNORECASE,
)

# Destructive shell/code that deletes or overwrites data when *executed*.
# Scanned against ADDED diff lines (committed code), where the narration
# patterns above ("deleted all", "wrote 0 bytes") don't apply but an actual
# `rm -rf` landing in the patch must still be caught. A ribosome can destroy
# data through committed code without ever narrating it to stdout.
_DESTRUCTION_SHELL_PATTERNS = _re.compile(
    r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f|rm\s+-[a-zA-Z]*f[a-zA-Z]*r|"
    r"\brmdir\b|git\s+clean\s+-[a-zA-Z]*f|shutil\.rmtree|"
    r"mkfs|dd\s+if=",
    _re.IGNORECASE,
)

# `fatal:` is anchored to start-of-line under MULTILINE (review-gate-3):
# genuine git/tool fatal errors are emitted line-leading, whereas an
# unanchored `fatal:` also matched benign mid-line prose narration and
# rejected good work. The other patterns are substantive enough to match
# anywhere.
_ERROR_PATTERNS = _re.compile(
    r"SyntaxError|ImportError|ModuleNotFoundError|PermissionError|"
    r"Traceback \(most recent|panic:|^fatal:",
    _re.IGNORECASE | _re.MULTILINE,
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
            "reflex_ban",
        )
    )


# A single-file deletion this large is a wholesale wipe, not an incidental
# trim in a refactor. The net_positive guard below suppresses the ratio-based
# pure_deletion/file_shrunk flags to avoid truncated-diff false positives, but
# it must NOT mask a genuine gutting of one file just because an unrelated
# larger file was added in the same change (review-gate-2). Above this line
# count, pure_deletion fires regardless of net_positive. Kept well above the
# modest (<=50 line) removals a legitimate net-positive refactor produces.
_LARGE_FILE_DELETION_LINES = 200

_PLACEHOLDER_PATTERNS = _re.compile(r"\bTODO\b|\bFIXME\b|\bstub\b", _re.IGNORECASE)
_HARDCODED_HOME = _re.compile(r"/Users/terry/|/home/terry/")
_PY2_EXCEPT = _re.compile(r"^\s*except\s+\w+\s*,\s*\w+\s*:", _re.MULTILINE)
_DUPE_FUTURE = _re.compile(r"from\s+__future__\s+import\s+annotations")
_FUTURE_IMPORT = _re.compile(r"from\s+__future__\s+import\s+(.+)")
_TEST_COMMAND = _re.compile(
    r"\b(?:uv\s+run\s+pytest|pytest|npm\s+test|pnpm\s+test|bun\s+test)\b[^\n\r]*",
    _re.IGNORECASE,
)
_TEST_PASSED = _re.compile(
    r"\b\d+\s+passed\b|\btests?\s+passed\b|\ball\s+tests\s+pass(?:ed)?\b",
    _re.IGNORECASE,
)
_REFLEX_BAN_PATTERNS: tuple[tuple[str, _re.Pattern[str]], ...] = (
    ("inline_bypass", _re.compile(r"#\s*(?:noqa|type:\s*ignore|pragma:\s*no\s*cover)\b")),
    ("todo_marker", _re.compile(r"#\s*(?:TODO|FIXME)\b", _re.IGNORECASE)),
    ("typing_optional", _re.compile(r"\b(?:from\s+typing\s+import\s+Optional|Optional\[)")),
    ("os_path_join", _re.compile(r"\bos\.path\.join\(")),
    ("per_class_logger", _re.compile(r"self\.logger\s*=\s*logging\.getLogger\(__name__\)")),
    # Promoted 2026-06-06 from the (formerly non-blocking) narration warning to a
    # diff-scoped blocking gate. A hardcoded macOS/linux home path in *committed*
    # code breaks portability across hosts (genome: "Never hardcode paths").
    # The narration `hardcoded_home_path` flag below stays as a non-blocking
    # warning — GLM mentioning a path in prose is not a defect; landing it is.
    ("hardcoded_home_path", _re.compile(r"/Users/terry/|/home/terry/")),
)


def _changed_paths_from_stat(stat_text: str) -> list[str]:
    """Extract changed file paths from git diff --stat text."""
    paths: list[str] = []
    for line in stat_text.splitlines():
        if "|" not in line:
            continue
        path = line.strip().split("|", 1)[0].strip()
        if path:
            paths.append(path)
    return paths


def _task_file_paths(task: str) -> set[str]:
    """Extract likely file paths mentioned in task text."""
    paths = set(_re.findall(r"[\w./~-]+\.(?:py|sh|toml|md|yaml|yml|json)", task))
    return {path for path in paths if not path.startswith("http") and len(path) > 4}


def _normalize_task_path(path: str) -> str:
    path = path.lstrip("~/")
    for prefix in ("germline/", "home/vivesca/germline/"):
        path = path.removeprefix(prefix)
    return path


def _detected_test_commands(output: str) -> list[str]:
    commands: list[str] = []
    for match in _TEST_COMMAND.findall(output):
        command = match.strip()
        if command and command not in commands:
            commands.append(command)
    return commands


def _added_patch_lines(patch_text: str) -> str:
    """Return added code lines from a unified diff, excluding diff metadata."""
    lines: list[str] = []
    for line in patch_text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        lines.append(line[1:])
    return "\n".join(lines)


def _dupe_future_in_diff(patch_text: str) -> bool:
    """True if any single file in the patch ADDS the same __future__ import twice.

    Walks the unified diff tracking the current target file via ``+++ b/...``
    headers and counts added ``from __future__ import <name>`` lines per file.
    A name appearing twice within one file's added lines is a genuine duplicate
    — the new-file case GLM produces (prepending a second
    ``from __future__ import annotations`` to a file it is creating).

    Limitation: when GLM adds a duplicate to a file that *already* had the
    import, the pre-existing line is diff context (not an added ``+`` line), so
    it is invisible here. ribosome-validate's full-file AST check covers that
    case; this diff-scoped gate covers the new-file case in the merge path.
    """
    current: str | None = None
    per_file: dict[str, list[str]] = {}
    for line in patch_text.splitlines():
        if line.startswith("+++ "):
            current = line[4:].strip()
            per_file.setdefault(current, [])
            continue
        if current is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            match = _FUTURE_IMPORT.search(line[1:])
            if match:
                per_file[current].extend(name.strip() for name in match.group(1).split(","))
    return any(len(names) != len(set(names)) for names in per_file.values())


def _reflex_ban_flags(patch_text: str) -> list[str]:
    """Return deterministic flags for promoted GLM coaching bans."""
    added = _added_patch_lines(patch_text)
    flags: list[str] = []
    for name, pattern in _REFLEX_BAN_PATTERNS:
        if pattern.search(added):
            flags.append(f"reflex_ban:{name}")

    if _re.search(r"except\s+Exception\s*:\s*\n\s*(?:pass|(?:self\.)?logger\.|logging\.|print\()", added):
        flags.append("reflex_ban:broad_exception_swallow")

    # Per-file duplicate __future__ import in the committed diff (blocking).
    if _dupe_future_in_diff(patch_text):
        flags.append("reflex_ban:dupe_future_import")

    return flags


def _dossier_operator_state(exit_code: int, verdict: str, approved: bool) -> str:
    """Return the terminal operator state visible from review evidence alone."""
    if approved:
        return "approved"
    if verdict == "incomplete":
        return "incomplete"
    if verdict == "rejected":
        return "failed_review" if exit_code == 0 else "failed_process"
    if exit_code != 0:
        return "failed_process"
    return "completed_unreviewed"


def _build_completion_dossier(
    result: dict,
    review: dict,
    completion_evidence: dict,
    *,
    approved: bool,
    verdict: str,
    score: int,
) -> dict:
    """Build the durable per-workflow evidence index."""
    workflow_id = str(result.get("workflow_id") or "")
    exit_code = int(result.get("exit_code", -1))
    artifact = completion_evidence.get("artifact", {})
    verification = completion_evidence.get("verification", {})
    decision = completion_evidence.get("decision", {})
    output_path = result.get("output_path", "") or review.get("output_path", "")
    cached_log_path = result.get("cached_log_path", "")
    operator_state = _dossier_operator_state(exit_code, verdict, approved)

    next_action = {}
    if workflow_id:
        if operator_state == "approved":
            next_action = {
                "command": f"mtor review {workflow_id}",
                "description": "Mark approved workflow as reviewed",
            }
        else:
            next_action = {
                "command": f"mtor logs {workflow_id}",
                "description": "Inspect preserved output",
            }

    return {
        "workflow_id": workflow_id,
        "repo_root": result.get("repo_root", ""),
        "base_sha": result.get("base_sha", ""),
        "requested_provider": result.get("requested_provider", result.get("provider", "")),
        "resolved_provider": result.get("provider", ""),
        "attempted_providers": result.get("attempted_providers", []),
        "mode": result.get("mode", "build"),
        "task_preview": result.get("task", "")[:200],
        "stderr": str(result.get("stderr", ""))[:500],
        "gate": result.get("gate", ""),
        "blocked_keyword": result.get("blocked_keyword", ""),
        "artifact": {
            "branch_name": result.get("branch_name", artifact.get("branch_name", "")),
            "commit_count": artifact.get("commit_count", 0),
            "commits": artifact.get("commits", []),
            "changed_paths": artifact.get("changed_paths", []),
            "has_patch": artifact.get("has_patch", False),
            "patch_bytes": artifact.get("patch_bytes", 0),
            "output_path": output_path,
            "cached_log_path": cached_log_path,
            "pr_url": result.get("pr_url", ""),
            "pr_number": result.get("pr_number", 0),
        },
        "verification": verification,
        "review": {
            "approved": approved,
            "verdict": verdict,
            "flags": review.get("flags", []),
            "blocking_flags": decision.get("blocking_flags", []),
            "warnings": decision.get("warnings", []),
            "satisfaction": score,
        },
        "operator": {
            "state": operator_state,
            "next_action": next_action,
        },
        "completion_evidence": completion_evidence,
        "created_at": review.get("ts", ""),
        "reviewed_at": review.get("ts", ""),
    }



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

    # Destruction is scanned in two places: narration in stdout/stderr, AND the
    # actual committed diff (added lines). Either one carrying a destructive
    # shell command is a hard reject downstream — see the verdict guard below.
    _post_diff_obj = result.get("post_diff", {})
    _scan_patch = _post_diff_obj.get("patch", "") if isinstance(_post_diff_obj, dict) else ""
    destruction_hits = _DESTRUCTION_PATTERNS.findall(combined)
    destruction_hits += _DESTRUCTION_SHELL_PATTERNS.findall(_added_patch_lines(_scan_patch))
    if destruction_hits:
        flags.append(f"destruction: {', '.join(list(dict.fromkeys(destruction_hits))[:3])}")

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
    flags.extend(_reflex_ban_flags(post_patch))
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

    changed_paths = _changed_paths_from_stat(post_stat_text)

    # Extract ALL file paths mentioned in the task and check if they appear in the diff.
    # Catches "task mentions dispatch.py but diff only touches cli.py" mismatches.
    task_files = _task_file_paths(task)
    missing_requested_paths: list[str] = []
    if task_files and exit_code == 0 and post_stat_text:
        diff_files = set(changed_paths)

        for task_file in task_files:
            norm = _normalize_task_path(task_file)
            if (
                norm
                # Path-segment match, not unbounded substring: requested
                # `api.py` must not be "satisfied" by an unrelated
                # `legacy_api.py` (review-gate-4). Either the diff path equals
                # the requested path, or the requested path is a trailing path
                # segment of it (preceded by `/`).
                and not any(
                    df == norm or df.endswith("/" + norm) for df in diff_files
                )
                and any(
                    kw in task.lower()
                    for kw in ["modify", "edit", "change", "add to", "update", "fix", "create"]
                )
            ):
                missing_requested_paths.append(norm)
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
                        # Non-suppressible escape hatch: a large absolute
                        # single-file wipe is dangerous even when the overall
                        # change is net-positive (review-gate-2).
                        if not net_positive or r > _LARGE_FILE_DELETION_LINES:
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

    # SAFETY INVARIANT — destruction is an unconditional hard reject.
    # This is deliberately the LAST word on the verdict: it runs after the
    # incomplete branch, after the scout/build approval logic, and after the
    # approved_with_flags upgrade. No mode, exit code, satisfaction score, or
    # future branch can let a diff that deletes data ("rm -rf", "deleted all",
    # shutil.rmtree, ...) reach an approve-class verdict. The implicit guard via
    # _is_blocking_review_flag already rejects today; this makes the invariant
    # explicit and impossible to regress by editing the branches above.
    if any(f.startswith("destruction") for f in flags):
        approved = False
        verdict = "rejected"

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

    detected_commands = _detected_test_commands(combined)
    verification_status = "unknown"
    if detected_commands:
        verification_status = "passed" if _TEST_PASSED.search(combined) else "detected"
    blocking_flags = [
        flag for flag in flags
        if flag.startswith("exit_code=") or _is_blocking_review_flag(flag)
    ]
    warnings = [flag for flag in flags if flag not in blocking_flags]

    completion_evidence = {
        "execution": {
            "provider": provider,
            "exit_code": exit_code,
            "mode": result.get("mode", "build"),
            "output_path": result.get("output_path", ""),
            "cached_log_path": result.get("cached_log_path", ""),
            "stderr": str(result.get("stderr", ""))[:500],
            "gate": result.get("gate", ""),
            "blocked_keyword": result.get("blocked_keyword", ""),
        },
        "artifact": {
            "branch_name": branch_name,
            "commit_count": commit_count,
            "commits": commits_list,
            "has_stat": bool(post_stat_text.strip()),
            "has_numstat": bool(post_numstat.strip()),
            "has_patch": bool(post_patch.strip()),
            "patch_bytes": len(post_patch.encode("utf-8")),
            "changed_paths": changed_paths,
        },
        "verification": {
            "status": verification_status,
            "detected_commands": detected_commands,
        },
        "scope": {
            "requested_paths": sorted(_normalize_task_path(path) for path in task_files),
            "changed_paths": changed_paths,
            "missing_requested_paths": sorted(set(missing_requested_paths)),
        },
        "decision": {
            "approved": approved,
            "verdict": verdict,
            "blocking_flags": blocking_flags,
            "warnings": warnings,
            "satisfaction": score,
        },
    }

    ts = _time.strftime("%Y-%m-%dT%H:%M:%S")
    review = {
        "ts": ts,
        "task": task[:200],
        "provider": provider,
        "exit_code": exit_code,
        "flags": flags,
        "verdict": verdict,
        "stdout_len": len(stdout),
        "stderr_len": len(stderr),
        "diff": post_stat[:500] if post_stat else "",
        "cost_info": result.get("cost_info", ""),
        "completion_evidence": completion_evidence,
    }
    if result.get("output_path"):
        review["output_path"] = result.get("output_path")
    if verdict == "incomplete" and branch_name:
        review["branch_name"] = branch_name

    completion_dossier = _build_completion_dossier(
        result,
        review,
        completion_evidence,
        approved=approved,
        verdict=verdict,
        score=score,
    )
    workflow_id = completion_dossier.get("workflow_id", "")
    if workflow_id:
        dossier_path = DOSSIER_DIR / f"{workflow_id}.json"
        try:
            DOSSIER_DIR.mkdir(parents=True, exist_ok=True)
            review["dossier_path"] = str(dossier_path)
            completion_dossier["dossier_path"] = str(dossier_path)
            dossier_path.write_text(
                json.dumps(completion_dossier, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    review["completion_dossier"] = completion_dossier

    try:
        REVIEW_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(REVIEW_LOG, "a") as f:
            f.write(json.dumps(review) + "\n")
    except OSError:
        pass

    return {
        "approved": approved,
        "flags": flags,
        "verdict": verdict,
        "satisfaction": score,
        "requeue_prompt": requeue_prompt,
        "completion_evidence": completion_evidence,
        "completion_dossier": completion_dossier,
        "dossier_path": review.get("dossier_path", ""),
    }
