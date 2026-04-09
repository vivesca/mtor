# mtor

Temporal-based task dispatch system. Dispatches coding tasks from soma (CLI) to ganglion (worker) via Temporal workflows. Named after the mTOR kinase — senses resources, gates growth.

## Architecture

```
soma (CLI)  →  Temporal (ganglion:7233)  →  translocase (activity)  →  ribosome (executor)
mtor/cli.py    mtor/worker/workflow.py      mtor/worker/translocase.py   ~/germline/effectors/ribosome
```

- **cli.py** — cyclopts CLI. All commands emit JSON via `_ok()` / `_err()` envelope.
- **dispatch.py** — core dispatch logic, spec injection, dedup, workflow ID generation.
- **spec.py** — spec frontmatter parser and updater. `update_spec_status()` is the write path.
- **worker/translocase.py** — Temporal activity. Spawns ribosome in worktrees, captures diffs, runs verdict review. ~1600 lines, largest file.
- **worker/workflow.py** — Temporal workflow definitions (TranslationWorkflow, WatchWorkflow).
- **worker/provider.py** — provider health, circuit breaker, round-robin selection, feedback tracking.
- **watch.py** — ganglion sync polling, pause/freeze (rapa/deptor), AMPK load sensing, feedback controller.

## Conventions

- **Tests in `assays/`** — flat directory, `test_<name>.py`. Run: `uv run pytest assays/ -x`
- **Test naming** — `def test_<description>()` with single `test_` prefix. Never `test_test_*`.
- **JSON envelope** — all CLI output uses `_ok(cmd, result, next_actions, version)` or `_err(cmd, msg, code, fix)`.
- **Specs** — YAML frontmatter in markdown files. Required fields: `status`, `repo`. `tests:` field required for dispatch.
- **Commits** — `feat:`, `fix:`, `test:`, `refactor:` prefixes. Include `Co-Authored-By` trailer.

## Key rules

- **Never delete existing functions** to replace with simpler versions. Add alongside. Specs are additive unless they explicitly say "replace" or "remove".
- **Never use `--no-tests`** — removed. Specs must have `tests:` frontmatter.
- **Spec status flow** — `ready` → `dispatched` → `done`/`failed`. `update_spec_status()` handles transitions.
- **Provider config** — canonical source: `~/germline/loci/ribosome-config.lock.json`. Model names are lowercase (`glm-5.1` not `GLM-5.1`).
- **Worktrees** — translocase creates per-task worktrees from main. Commits land on `ribosome-*` branches, auto-merged to main if verdict approved.
- **PROVIDER_LIMITS** in translocase.py — max concurrent per provider. Currently `{"zhipu": 2}`.

## Running

```bash
# CLI (soma)
mtor --spec <spec.md> "prompt"    # dispatch
mtor list                          # list workflows
mtor status <workflow_id>          # detail
mtor logs <workflow_id>            # fetch worker logs

# Worker (ganglion)
cd ~/code/mtor && source ~/.env.bootstrap && op run --env-file ~/germline/loci/env.op -- python -m mtor.worker

# Tests
cd ~/code/mtor && uv run pytest assays/ -x --tb=short
```

## Gotchas

- `scope:` in spec frontmatter can be a bare string (`scope: mtor`) or list — `_inject_spec_constraints` handles both.
- Dedup uses prompt text + spec path hash. Same prompt + different spec = different key. Same prompt + same spec within 5 min = blocked.
- `_auto_commit()` in translocase runs after successful ribosome exit — safety net for GLM forgetting to commit.
- Verdict gate flags: `no_commit_on_success`, `destruction`, `target_file_missing`, `nested_test_file`. Any flag → rejected unless scout mode.
