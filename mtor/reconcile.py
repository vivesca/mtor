"""Spec status reconciliation — derive status from Temporal and git reality."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from mtor.client import _get_client
from mtor.spec import update_spec_status
from mtor.rptor import parse_spec, scan_specs

ACCEPTED_VERDICTS = {"accepted", "approved", "approved_with_flags", "early_exit_clean"}

# Worker/remote home roots that map onto the local home directory. A completion
# dossier produced on the ganglion worker records absolute paths under the worker
# home (/home/vivesca). When reconciling locally those roots must be re-rooted to
# the local home so existing files are not falsely reported missing.
_REMOTE_HOME_ROOTS = ("/home/vivesca", "/root")


def _candidate_paths(path_part: str, repo_root: Path) -> list[Path]:
    """Resolve a spec file entry to candidate filesystem paths to probe.

    - strips surrounding markdown inline-code backticks and whitespace
    - "~/..." → expand against the local home
    - absolute "/..." → the literal path, plus a local-home remap for any known
      remote/worker home root so a remote dossier root does not poison the check
    - otherwise → resolved against ``repo_root``
    """
    path_part = path_part.strip().strip("`").strip()
    if not path_part:
        return []

    candidates: list[Path] = []
    if path_part.startswith("~"):
        candidates.append(Path(path_part).expanduser())
    elif path_part.startswith("/"):
        candidates.append(Path(path_part))
        home = Path.home()
        for root in _REMOTE_HOME_ROOTS:
            if path_part == root or path_part.startswith(root + "/"):
                rel = path_part[len(root) :].lstrip("/")
                if rel:
                    candidates.append(home / rel)
    else:
        candidates.append(repo_root / path_part)
    return candidates


def check_code_exists(
    file_or_function: str, repo_root: Path = Path.home() / "code" / "mtor"
) -> bool:
    """Check if a function or file exists in the codebase.

    Supports:
    - "file.py" → check if file exists
    - "file.py:func_name" → check if file exists and function is defined
    - "module.file.py:ClassName.method" → check file and method/function

    Entries may be wrapped in markdown inline-code backticks and may carry a
    remote/worker home root; both are normalised before probing the filesystem.
    """
    raw = file_or_function.strip().strip("`").strip()
    parts = raw.split(":")
    path_part = parts[0]
    func_name = parts[1].strip().strip("`").strip() if len(parts) > 1 else None

    file_path = None
    for candidate in _candidate_paths(path_part, repo_root):
        if candidate.exists():
            file_path = candidate
            break

    if file_path is None:
        return False

    if not func_name:
        return True

    # Search file for function or method definition
    try:
        content = file_path.read_text(encoding="utf-8")
        # Patterns: def func, async def func, class class, def class.method
        patterns = [
            rf"def\s+{re.escape(func_name)}\s*\(",
            rf"async\s+def\s+{re.escape(func_name)}\s*\(",
            rf"class\s+{re.escape(func_name)}\s*[\(:]",
        ]
        for pattern in patterns:
            if re.search(pattern, content, re.MULTILINE):
                return True
        return False
    except Exception:
        return False


async def describe_workflow(workflow_id: str, client: Any | None = None):
    """Return a workflow description for a workflow ID, or None if unavailable."""
    if not workflow_id:
        return None
    if client is None:
        client, err = _get_client()
        if err:
            return None
    try:
        handle = client.get_workflow_handle(workflow_id)
        return await handle.describe()
    except Exception:
        return None


def _status_name(execution: Any) -> str:
    status = getattr(execution, "status", None)
    return getattr(status, "name", "UNKNOWN") if status else "UNKNOWN"


def _search_attr_value(execution: Any, wanted_key: str) -> str:
    attrs = getattr(execution, "search_attributes", None)
    if not attrs:
        return ""
    try:
        items = attrs.items()
    except AttributeError:
        items = []
    for key, value in items:
        if wanted_key not in str(key):
            continue
        if isinstance(value, list):
            return str(value[0]) if value else ""
        return str(value)
    return ""


def has_commit_for_spec(spec_name: str) -> bool:
    """Check if there are commits referencing this spec on main branch."""
    import subprocess

    result = subprocess.run(
        ["git", "log", "--oneline", "main", f"--grep={spec_name}", "-n", "1"],
        capture_output=True,
        text=True,
        cwd=Path.home() / "code" / "mtor",
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def reconcile_spec(
    spec: dict[str, Any],
    dry_run: bool = False,
    client: Any | None = None,
    workflow_description: Any | None = None,
) -> dict[str, Any]:
    """Reconcile a single spec's status against Temporal and git.

    Follows the spec feedback loop:
    1. If status == "dispatched" and workflow_id exists:
       - RUNNING: skip
       - COMPLETED + accepted verdict: done
       - COMPLETED + rejected verdict: failed
       - TERMINATED/CANCELED/FAILED: failed
       - missing workflow: ready, clear workflow_id

    2. If status == "done":
       - if spec lists files/functions in "Files to edit": check if code exists
       - if none found: warn "status=done but code not found"

    3. If status == "ready" and depends_on is set:
       - check each dependency's status
       - if any dep is not done: mark as blocked
    """
    result = {
        "name": spec.get("name", "unknown"),
        "path": spec.get("path", ""),
        "was": spec.get("status", "unknown"),
        "now": spec.get("status", "unknown"),
        "reason": None,
        "warning": None,
        "changed": False,
        "blocked_by": None,
    }

    status = spec.get("status", "").strip().lower()
    spec_path = Path(spec.get("path", "")) if spec.get("path") else None
    spec_name = spec.get("name", "")

    if status == "dispatched":
        workflow_id = str(spec.get("workflow_id", "") or "")
        if not workflow_id:
            return result
        verdict = ""

        latest = workflow_description
        if latest is None:
            latest = asyncio.run(describe_workflow(workflow_id, client=client))
        if latest is None:
            new_status = "ready"
            result["now"] = new_status
            result["reason"] = "workflow not found"
            result["changed"] = True
        else:
            latest_status = _status_name(latest)
            verdict = str(spec.get("verdict", "") or "") or _search_attr_value(
                latest, "mtor_verdict"
            )

            if latest_status == "RUNNING":
                return result
            if latest_status == "COMPLETED":
                if verdict in ACCEPTED_VERDICTS:
                    new_status = "done"
                    result["now"] = new_status
                    result["reason"] = f"workflow completed with {verdict} verdict"
                    result["changed"] = True
                elif verdict == "rejected":
                    new_status = "failed"
                    result["now"] = new_status
                    result["reason"] = "workflow completed with rejected verdict"
                    result["changed"] = True
                elif has_commit_for_spec(spec_name):
                    new_status = "done"
                    result["now"] = new_status
                    result["reason"] = "workflow completed with commits on main"
                    result["changed"] = True
                else:
                    new_status = "ready"
                    result["now"] = new_status
                    result["reason"] = (
                        "workflow completed but verdict/commits were not found"
                    )
                    result["changed"] = True
            elif latest_status in {"TERMINATED", "CANCELED", "FAILED"}:
                new_status = "failed"
                result["now"] = new_status
                result["reason"] = f"workflow was {latest_status.lower()}"
                result["changed"] = True
            else:
                return result

        # Apply change if needed
        if result["changed"] and not dry_run and spec_path is not None:
            if new_status == "ready":
                update_spec_status(spec_path, new_status, clear_workflow_id=True)
            else:
                update_spec_status(
                    spec_path,
                    new_status,
                    verdict=verdict or None,
                    audit_reason=result["reason"] if new_status == "failed" else None,
                )

    elif status == "done":
        # Check if "Files to edit" section exists in body
        body = spec.get("body", "")
        if not body:
            return result

        # Find "Files to edit" or "files to edit" section
        files_match = re.search(
            r"##\s*(Files? (?:to modify|to change|to edit)|File list)\s*\n(.*?)(?:\n##|\Z)",
            body,
            re.DOTALL | re.IGNORECASE,
        )
        if not files_match:
            return result

        files_text = files_match.group(2).strip()
        if not files_text:
            return result

        # Parse list items (lines starting with - or just one per line)
        file_entries = []
        for line in files_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                entry = stripped[2:].strip()
                if entry:
                    file_entries.append(entry)
            elif stripped.startswith("-"):
                entry = stripped[1:].strip()
                if entry:
                    file_entries.append(entry)
            elif stripped:
                file_entries.append(stripped)

        # Check each entry
        missing = []
        for entry in file_entries:
            if not check_code_exists(entry):
                missing.append(entry)

        if missing:
            result["warning"] = (
                f"status=done but {len(missing)} file(s)/function(s) not found: {', '.join(missing)}"
            )

    elif status in ("ready", "blocked") and "depends_on" in spec:
        depends_on = spec.get("depends_on", [])
        if depends_on:
            # We'll need all specs to check dependency status — handled at higher level
            pass

    return result


def reconcile_workflow_specs(
    executions: list[Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Reconcile the exact spec paths carried by workflow search attributes.

    The path recorded in ``mtor_spec`` is authoritative. This avoids scanning a
    historical default directory and also supports specs dispatched from an
    explicit directory. A stale workflow cannot overwrite a newer dispatch of
    the same spec because its workflow ID must still match the frontmatter.
    """
    fixed: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    correct = 0
    seen: set[Path] = set()

    for execution in executions:
        workflow_id = str(getattr(execution, "id", "") or "")
        raw_path = _search_attr_value(execution, "mtor_spec")
        if not workflow_id or not raw_path:
            continue
        spec_path = Path(raw_path).expanduser()
        if spec_path in seen:
            continue
        seen.add(spec_path)
        if not spec_path.is_file():
            skipped.append({"workflow_id": workflow_id, "reason": "spec_not_found"})
            continue
        try:
            spec = parse_spec(spec_path)
        except (OSError, ValueError):
            skipped.append({"workflow_id": workflow_id, "reason": "spec_invalid"})
            continue
        if str(spec.get("workflow_id", "") or "") != workflow_id:
            skipped.append({"workflow_id": workflow_id, "reason": "workflow_mismatch"})
            continue

        result = reconcile_spec(
            spec,
            dry_run=dry_run,
            workflow_description=execution,
        )
        if result["changed"]:
            fixed.append(
                {
                    "name": result["name"],
                    "was": result["was"],
                    "now": result["now"],
                    "reason": result["reason"],
                }
            )
        else:
            correct += 1

    return {
        "scanned": len(seen),
        "fixed": fixed,
        "correct": correct,
        "skipped": skipped,
        "dry_run": dry_run,
    }


def reconcile_all(
    spec_dir: Path,
    dry_run: bool = False,
    client: Any | None = None,
    workflow_descriptions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reconcile all specs in a directory.

    Returns JSON-compatible result with statistics and fixes.
    """
    scanned = scan_specs(spec_dir)
    spec_map = {s["name"]: s for s in scanned}

    scanned_count = len(scanned)
    fixed: list[dict[str, Any]] = []
    correct = 0
    blocked: list[str] = []

    for spec in scanned:
        workflow_id = str(spec.get("workflow_id", "") or "")
        if (
            workflow_descriptions is not None
            and workflow_id not in workflow_descriptions
        ):
            correct += 1
            continue
        result = reconcile_spec(
            spec,
            dry_run=dry_run,
            client=client,
            workflow_description=workflow_descriptions.get(workflow_id)
            if workflow_descriptions
            else None,
        )
        if result["changed"]:
            fixed.append(
                {
                    "name": result["name"],
                    "was": result["was"],
                    "now": result["now"],
                    "reason": result["reason"],
                }
            )
        elif result["warning"]:
            fixed.append(
                {
                    "name": result["name"],
                    "was": result["was"],
                    "now": result["now"],
                    "warning": result["warning"],
                }
            )
        else:
            correct += 1

        # Check dependencies if currently ready
        status = spec.get("status", "").strip().lower()
        if status in ("ready", "blocked") and "depends_on" in spec:
            depends_on = spec.get("depends_on", [])
            any_not_done = False
            for dep_name in depends_on:
                dep_spec = spec_map.get(dep_name)
                if not dep_spec or dep_spec.get("status", "done") != "done":
                    any_not_done = True
                    break
            if any_not_done:
                blocked.append(f"{result['name']} (needs {dep_name})")

    return {
        "scanned": scanned_count,
        "fixed": fixed,
        "correct": correct,
        "blocked": blocked,
        "dry_run": dry_run,
    }
