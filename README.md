# mtor

Agent-first coding task dispatcher for Temporal workflows. Dispatches AI coding tasks to workers, reviews results automatically, and merges approved changes.

## Install

```bash
uvx mtor
# or
pip install mtor
```

## Quick start

```bash
# Dispatch a coding task
mtor "Add logging to server.py"

# Check status
mtor status ribosome-zhipu-abc123

# List recent runs
mtor list --since 24

# Health check
mtor doctor
```

## Configuration

All config via environment variables:

| Variable | Default | Description |
|---|---|---|
| `MTOR_DURABLE_BACKEND` | `temporal` | Durable backend selector. This release enables only `temporal` and fails closed on every other value. |
| `MTOR_TEMPORAL_HOST` | `ganglion:7233` | Temporal server address used by the CLI. |
| `TEMPORAL_HOST` | `ganglion:7233` | Temporal server address used by the existing worker service. |

Other settings (task queue, output paths) are configured in `mtor/__init__.py`.

## Features

### Auto-routing

Tasks are automatically routed to the best provider based on keywords:

- Exploration queries → `zhipu`
- Bug fixes → `zhipu`
- Build tasks → `zhipu`

Override with `--provider`:

```bash
mtor "Fix the login bug" --provider zhipu
```

### Risk classification

Tasks are classified as low / medium / high risk. High-risk tasks (deletions, config changes) are flagged in the dispatch response.

### Spec file support

Pass a markdown file as the prompt — mtor reads it and strips YAML frontmatter:

```bash
mtor path/to/spec.md
```

### Spec decomposition

Multi-task specs (with `## Task 1`, `## Task 2` sections) are automatically split into individual workflows sharing the same preamble.

### Dedup

Same prompt → same workflow ID. Temporal prevents duplicate dispatches natively.

### Experiment mode

```bash
mtor -x "Try a new approach to caching"
```

Experiment tasks don't auto-merge — they stay on branches for manual review.

### Deferred approvals

Gate high-risk tasks behind manual approval:

```bash
mtor approve ribosome-zhipu-abc123
mtor deny ribosome-zhipu-abc123
```

## Commands

| Command | Description |
|---|---|
| `mtor "prompt"` | Dispatch a task |
| `mtor list` | List recent workflows |
| `mtor status <id>` | Get workflow details |
| `mtor logs <id>` | Fetch last 30 lines of output |
| `mtor cancel <id>` | Cancel a workflow |
| `mtor approve <id>` | Approve a deferred task |
| `mtor deny <id>` | Deny a deferred task |
| `mtor doctor` | Health check |
| `mtor deploy` | Sync the germline content repository, then restart the worker |
| `mtor rictor deploy` | Sync mTOR worker code, restart services, and verify health |
| `mtor history` | Recent run history from JSONL log |
| `mtor checkpoints` | List saved checkpoints from failed runs |
| `mtor schema` | Emit JSON schema of all commands |

Every response is a JSON envelope with `ok`, `command`, `result`/`error`, and `next_actions` — designed for programmatic consumption by agents and tools.

## Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Generic error |
| 2 | Usage error (missing required arguments) |
| 3 | Backend unsupported or Temporal unreachable |
| 4 | Workflow not found |

## Requirements

- Python ≥ 3.11
- A running [Temporal](https://temporal.io) server
- A worker process executing coding tasks (e.g., ribosome + translocase)

## License

MIT
