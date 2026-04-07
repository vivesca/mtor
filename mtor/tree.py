"""Command tree definition for agent self-discovery."""

from __future__ import annotations

from porin import CommandTree

from mtor import LOG_TAIL_LINES

tree = CommandTree("mtor")
tree.add_command(
    "mtor",
    "Bare invocation — returns this JSON command tree for agent self-discovery",
    params=[],
    annotations={"readonly": True},
)
tree.add_command(
    "mtor <prompt>",
    "Dispatch a task prompt to Temporal for agent execution.",
    params=[
        {
            "name": "prompt",
            "type": "string",
            "required": True,
            "description": "Task instruction for the ribosome worker",
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
    "mtor list",
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
    "mtor status <workflow_id>",
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
    "mtor logs <workflow_id>",
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
    "mtor cancel <workflow_id>",
    "Cancel a running workflow. Idempotent — cancelling an already-cancelled workflow is ok.",
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
        "result": {"workflow_id": "string", "cancelled": "boolean"},
        "next_actions": "array",
    },
    annotations={"readonly": False, "destructive": False, "idempotent": True},
)
tree.add_command(
    "mtor approve <workflow_id>",
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
    "mtor deny <workflow_id>",
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
    "mtor doctor",
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
    "mtor schema",
    "Emit full JSON schema of all commands with params, types, enums, defaults",
    params=[],
    returns={"schema_version": "string", "commands": "array"},
    annotations={"readonly": True},
)
