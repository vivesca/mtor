"""Command tree definition for agent self-discovery."""

from __future__ import annotations

from porin import CommandTree

from mtor import LOG_TAIL_LINES

tree = CommandTree("mtor")
tree.add_command(
    "",
    "Bare invocation — returns this JSON command tree for agent self-discovery",
    params=[],
    annotations={"readonly": True},
)
tree.add_command(
    "<prompt>",
    "Dispatch a task prompt to Temporal for agent execution.",
    params=[
        {
            "name": "prompt",
            "type": "string",
            "required": True,
            "description": "Task instruction for the ribosome worker",
        },
        {
            "name": "--spec",
            "type": "string",
            "required": False,
            "description": "Path to spec file — auto-updates status on dispatch",
        }
    ],
    returns={
        "ok": "boolean",
        "command": "string",
        "result": {
            "workflow_id": "string",
            "status": "string",
            "prompt_preview": "string (first 100 chars of dispatched prompt)",
        },
        "next_actions": "array",
    },
    annotations={"readonly": False, "destructive": False},
)
tree.add_command(
    "list",
    "List recent workflows with optional filters",
    params=[
        {
            "name": "--status",
            "type": "enum",
            "enum": ["RUNNING", "COMPLETED", "FAILED", "CANCELED", "TERMINATED"],
            "required": False,
            "default": None,
            "description": "Filter by execution status",
        },
        {
            "name": "--count",
            "type": "integer",
            "required": False,
            "default": 10,
            "description": "Maximum workflows to return",
        },
    ],
    returns={
        "ok": "boolean",
        "command": "string",
        "result": {
            "workflows": "array of {workflow_id, status, start_time, close_time}",
            "count": "integer",
        },
        "next_actions": "array (one mtor status per workflow)",
    },
    annotations={"readonly": True},
)
tree.add_command(
    "status <workflow_id>",
    "Get detailed status of a single workflow",
    params=[
        {
            "name": "workflow_id",
            "type": "string",
            "required": True,
            "description": "Temporal workflow ID",
        }
    ],
    returns={
        "ok": "boolean",
        "command": "string",
        "result": {
            "workflow_id": "string",
            "status": "string",
            "start_time": "string (ISO8601)",
            "close_time": "string or null",
        },
        "next_actions": "array",
    },
    errors=[
        {"code": "WORKFLOW_NOT_FOUND", "exit_code": 4, "message": "No workflow with that ID"},
        {
            "code": "TEMPORAL_UNREACHABLE",
            "exit_code": 3,
            "message": "Cannot connect to Temporal server",
        },
    ],
    annotations={"readonly": True},
)
tree.add_command(
    "logs <workflow_id>",
    f"Fetch last {LOG_TAIL_LINES} lines of workflow output from worker host",
    params=[
        {
            "name": "workflow_id",
            "type": "string",
            "required": True,
            "description": "Temporal workflow ID",
        }
    ],
    returns={
        "ok": "boolean",
        "command": "string",
        "result": {
            "lines": "array of strings",
            "log_path": "string (full path on worker host)",
            "truncated": "boolean",
        },
        "next_actions": "array",
    },
    annotations={"readonly": True},
)
tree.add_command(
    "terminate <workflow_id>",
    "Immediately terminate a running workflow. Idempotent.",
    params=[
        {
            "name": "workflow_id",
            "type": "string",
            "required": True,
            "description": "Temporal workflow ID",
        }
    ],
    returns={
        "ok": "boolean",
        "command": "string",
        "result": {"workflow_id": "string", "terminated": "boolean"},
        "next_actions": "array",
    },
    annotations={"readonly": False, "destructive": False, "idempotent": True},
)
tree.add_command(
    "cancel <workflow_id>",
    "Cancel a running workflow. Delegates to terminate for immediate stop. Idempotent.",
    params=[
        {
            "name": "workflow_id",
            "type": "string",
            "required": True,
            "description": "Temporal workflow ID",
        }
    ],
    returns={
        "ok": "boolean",
        "command": "string",
        "result": {"workflow_id": "string", "terminated": "boolean"},
        "next_actions": "array",
    },
    annotations={"readonly": False, "destructive": False, "idempotent": True},
)
tree.add_command(
    "approve <workflow_id>",
    "Approve a deferred (SRP-paused) ribosome task. Sends approval signal to Temporal.",
    params=[
        {
            "name": "workflow_id",
            "type": "string",
            "required": True,
            "description": "Temporal workflow ID",
        }
    ],
    annotations={"readonly": False, "destructive": False},
)
tree.add_command(
    "deny <workflow_id>",
    "Deny a deferred (SRP-paused) ribosome task. Sends rejection signal to Temporal.",
    params=[
        {
            "name": "workflow_id",
            "type": "string",
            "required": True,
            "description": "Temporal workflow ID",
        }
    ],
    annotations={"readonly": False, "destructive": False},
)
tree.add_command(
    "nudge <workflow_id>",
    "Send reactivation signal to a dormant workflow",
    params=[
        {
            "name": "workflow_id",
            "type": "string",
            "required": True,
            "description": "Temporal workflow ID",
        }
    ],
    returns={
        "ok": "boolean",
        "command": "string",
        "result": {"workflow_id": "string", "nudged": "boolean"},
        "next_actions": "array",
    },
    errors=[
        {"code": "WORKFLOW_NOT_FOUND", "exit_code": 4, "message": "No workflow with that ID"},
        {
            "code": "TEMPORAL_UNREACHABLE",
            "exit_code": 3,
            "message": "Cannot connect to Temporal server",
        },
    ],
    annotations={"readonly": False, "destructive": False},
)
tree.add_command(
    "doctor",
    "Health check: Temporal server reachability, worker liveness, provider info",
    params=[],
    returns={
        "ok": "boolean",
        "command": "string",
        "result": {
            "temporal_reachable": "boolean",
            "temporal_host": "string",
            "worker_alive": "boolean",
            "checks": "array of {name, ok, detail}",
        },
        "next_actions": "array",
    },
    annotations={"readonly": True},
)
tree.add_command(
    "schema",
    "Emit full JSON schema of all commands with params, types, enums, defaults",
    params=[],
    returns={"schema_version": "string", "commands": "array"},
    annotations={"readonly": True},
)
tree.add_command(
    "scout <prompt>",
    "Dispatch a read-only analysis task. Returns findings, not code.",
    params=[
        {
            "name": "prompt",
            "type": "string",
            "required": True,
            "description": "Analysis instruction",
        },
        {
            "name": "--provider",
            "type": "string",
            "required": False,
            "default": None,
            "description": "Override default provider",
        },
        {
            "name": "--wait/--no-wait",
            "type": "boolean",
            "required": False,
            "default": True,
            "description": "Wait for completion and print logs",
        },
        {
            "name": "--timeout",
            "type": "integer",
            "required": False,
            "default": 300,
            "description": "Max seconds to wait",
        },
    ],
    annotations={"readonly": True},
)
tree.add_command(
    "research <prompt>",
    "Dispatch an external research task. Searches web, synthesizes findings.",
    params=[
        {
            "name": "prompt",
            "type": "string",
            "required": True,
            "description": "Research question",
        },
        {
            "name": "--provider",
            "type": "string",
            "required": False,
            "default": None,
            "description": "Override default provider",
        },
        {
            "name": "--wait/--no-wait",
            "type": "boolean",
            "required": False,
            "default": True,
            "description": "Wait for completion and print logs",
        },
        {
            "name": "--timeout",
            "type": "integer",
            "required": False,
            "default": 600,
            "description": "Max seconds to wait",
        },
    ],
    annotations={"readonly": True},
)
tree.add_command(
    "scan",
    "Run deterministic checks: TODO/FIXME, missing tests, stale marks.",
    params=[],
    annotations={"readonly": True},
)
tree.add_command(
    "watch",
    "Poll ganglion remote and auto-sync new commits.",
    params=[
        {
            "name": "--interval",
            "type": "integer",
            "required": False,
            "default": 60,
            "description": "Seconds between sync attempts",
        },
        {
            "name": "--once",
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "Run exactly one sync cycle",
        },
        {
            "name": "--max-cycles",
            "type": "integer",
            "required": False,
            "default": None,
            "description": "Stop after N cycles (default: unlimited)",
        },
    ],
    returns={
        "ok": "boolean",
        "command": "string",
        "result": {
            "cycles": "integer",
            "total_fetched": "integer",
            "total_merged": "integer",
            "total_errors": "integer",
        },
        "next_actions": "array",
    },
    annotations={"readonly": False, "destructive": False},
)
tree.add_command(
    "pause",
    "Pause dispatching — blocks new tasks and watch sync cycles.",
    params=[],
    returns={
        "ok": "boolean",
        "result": {"status": "string", "pause_file": "string"},
        "next_actions": "array",
    },
    annotations={"readonly": False, "destructive": False},
)
tree.add_command(
    "resume",
    "Resume dispatching — removes pause marker.",
    params=[],
    returns={
        "ok": "boolean",
        "result": {"status": "string", "was_paused": "boolean"},
        "next_actions": "array",
    },
    annotations={"readonly": False, "destructive": False},
)
tree.add_command(
    "auto",
    "Scan for improvement opportunities and dispatch self-targeted tasks.",
    params=[
        {
            "name": "--dry-run",
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "Preview without dispatching",
        },
        {
            "name": "--provider",
            "type": "string",
            "required": False,
            "default": None,
            "description": "Override default provider",
        },
    ],
    annotations={"readonly": False},
)
tree.add_command(
    "mtor review <workflow_id>",
    "Mark a completed task as reviewed — seen, verdict noted.",
    params=[
        {
            "name": "workflow_id",
            "type": "string",
            "required": False,
            "description": "Temporal workflow ID (omit with --all)",
        },
        {
            "name": "--all",
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "Review all completed non-running tasks",
        },
    ],
    annotations={"readonly": False},
)
tree.add_command(
    "mtor archive <workflow_id>",
    "Archive a reviewed/completed task — hide from default list.",
    params=[
        {
            "name": "workflow_id",
            "type": "string",
            "required": False,
            "description": "Temporal workflow ID",
        },
        {
            "name": "--before",
            "type": "string",
            "required": False,
            "default": None,
            "description": "Archive completed workflows older than duration (e.g. 3h, 1d, 30m)",
        },
        {
            "name": "--all-reviewed",
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "Archive all reviewed tasks",
        },
    ],
    annotations={"readonly": False},
)
tree.add_command(
    "autophagy",
    "Salvage new commits from ganglion, cherry-pick, and run tests.",
    params=[
        {
            "name": "--repo",
            "type": "string",
            "required": False,
            "default": None,
            "description": "Repository path (defaults to REPO_DIR)",
        },
        {
            "name": "--no-test",
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "Skip test run after cherry-pick",
        },
    ],
    returns={
        "ok": "boolean",
        "command": "string",
        "result": {
            "fetched": "integer",
            "cherry_picked": "array of strings (commit SHAs)",
            "skipped": "array of strings (already-merged SHAs)",
            "tests_passed": "boolean",
            "error": "string or null",
        },
        "next_actions": "array",
    },
    annotations={"readonly": False, "destructive": False},
)
tree.add_command(
    "init <name>",
    "Scaffold a new spec file with YAML frontmatter.",
    params=[
        {
            "name": "name",
            "type": "string",
            "required": True,
            "description": "Spec name in kebab-case (becomes Title Case title)",
        },
        {
            "name": "--repo",
            "type": "string",
            "required": False,
            "default": None,
            "description": "Repository path (auto-detected from git root if omitted)",
        },
        {
            "name": "--scope",
            "type": "string",
            "required": False,
            "default": None,
            "description": "Comma-separated list of scope entries",
        },
        {
            "name": "--exclude",
            "type": "string",
            "required": False,
            "default": None,
            "description": "Comma-separated list of excluded files/directories",
        },
        {
            "name": "--dir",
            "type": "string",
            "required": False,
            "default": ".",
            "description": "Output directory (default: current directory)",
        },
    ],
    annotations={"readonly": False},
)
tree.add_command(
    "plan",
    "Display spec DAG — status, dependencies, and dispatchability",
    params=[
        {
            "name": "--dir",
            "type": "string",
            "required": False,
            "default": "~/epigenome/chromatin/loci/plans/",
            "description": "Directory containing spec .md files",
        },
        {
            "name": "--pending",
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "Show only dispatchable (ready) specs",
        },
    ],
    returns={
        "ok": "boolean",
        "result": {
            "specs": "object (buckets) or empty array",
            "counts": "object (per-bucket integer counts)",
            "directory": "string",
        },
    },
    annotations={"readonly": True},
)
tree.add_command(
    "infra check",
    "Infrastructure health check — worker SSH, repo, git, disk",
    params=[],
    returns={
        "ok": "boolean",
        "checks": "array of {name, ok, detail}",
    },
    annotations={"readonly": True},
)
tree.add_command(
    "infra deploy",
    "Sync code to worker, restart services, verify health",
    params=[],
    returns={
        "ok": "boolean",
        "steps": "array of {step, ok}",
        "healthy": "boolean",
        "error": "string or null",
    },
    annotations={"readonly": False, "destructive": False},
)
tree.add_command(
    "infra clean",
    "Remove old output and checkpoint files",
    params=[
        {
            "name": "--older-than-days",
            "type": "integer",
            "required": False,
            "default": 7,
            "description": "Remove files older than N days",
        },
    ],
    returns={
        "ok": "boolean",
        "outputs_removed": "integer",
        "checkpoints_removed": "integer",
        "errors": "array of strings",
    },
    annotations={"readonly": False, "destructive": True},
)
