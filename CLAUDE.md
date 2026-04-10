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
- **worker/translocase.py** — Temporal activity. Spawns ribosome in worktrees, captures diffs, runs verdict review. ~1500 lines, largest file.
- **worker/workflow.py** — Temporal workflow definitions (TranslationWorkflow, WatchWorkflow).
- **worker/provider.py** — provider health, circuit breaker, round-robin selection, feedback tracking.
- **watch.py** — ganglion sync polling, pause/freeze (rapa/deptor), AMPK load sensing, feedback controller.

## Conventions

- **Tests in `assays/`** — flat directory, `test_<name>.py`. Run: `uv run pytest assays/ -x`
- **Test naming** — `def test_<description>()` with single `test_` prefix. Never `test_test_*`.
- **JSON envelope** — all CLI output uses `_ok(cmd, result, next_actions, version)` or `_err(cmd, msg, code, fix)`.
- **Specs** — YAML frontmatter in markdown files. Required fields: `status`, `repo`. `tests:` field required for dispatch.
- **Commits** — `feat:`, `fix:`, `test:`, `refactor:` prefixes. Include `Co-Authored-By` trailer.
- **`git add -A` is forbidden.** Use explicit file paths.

## Key rules

- **Never delete existing functions** to replace with simpler versions. Add alongside. Specs are additive unless they explicitly say "replace" or "remove".
- **Execute the spec, not adjacent work.** Only touch files listed in the spec. Everything else is wasted work.
- **Read the original file fully** before rewriting. Don't guess at what exists.
- **After changes, ALWAYS commit:** `git add <files> && git commit -m 'ribosome: <what changed>'`. Uncommitted work is invisible work.
- **Verify before claiming done:** run tests, import modules, read back files.
- **Spec status flow** — `ready` → `dispatched` → `done`/`failed`. `update_spec_status()` handles transitions.
- **Worktrees** — translocase creates per-task worktrees from main. Commits land on `ribosome-*` branches, pushed to origin for review. Never commit to main directly.
- **PROVIDER_LIMITS** in translocase.py — max concurrent per provider. Currently `{"zhipu": 2}`.

## Code patterns

- **No hallucinated imports.** Only import functions that already exist.
- **Preserve return types.** Don't flatten distinct result classes into one generic.
- **Python 3 except syntax only.** `except (A, B):` not `except A, B:`.
- **Mock where looked up, not defined.** Read imports first.
- **Never duplicate `from __future__ import annotations`.** One instance only, at the top.
- **"Edit a file" ≠ "rewrite a file."** READ first, then PATCH. Never full rewrite from memory for files >20 lines.

## Temporal rules

- **NEVER `asyncio.sleep` in workflow code.** Use `workflow.sleep()`. Breaks replay determinism.
- **NEVER `time.time()` in workflow code.** Use `workflow.now()`.
- **NEVER `Path.home()` in workflow code.** Non-deterministic. Pass paths as workflow input.
- **Imports in workflow methods** MUST use `workflow.unsafe.imports_passed_through()`.

## Testing

- **Tests go in `assays/` flat.** NEVER mirror source directory structure.
- **Unique test file names required.** Generic module → prefix with domain: `test_rss_config.py`.
- **Run:** `cd ~/code/mtor && uv run pytest <file> -v --tb=short`. Never bare `python`.

## Environment

- **Working directory is the repo root.** Don't assume cwd is a subdirectory.
- **Home is `/home/vivesca/`.** Use `Path.home()` or `$HOME`. Never hardcode paths.
- **stdin is /dev/null.** All data must come from files or arguments.
- **Never echo secrets.** Use `test -n "$VAR"`, not `echo $VAR | head`.
- **Provider config** — canonical source: `~/germline/loci/ribosome-config.lock.json`. Model names are lowercase (`glm-5.1` not `GLM-5.1`).

## Transient coaching (promote to permanent or retire)

- **Write the output file FIRST.** Skeleton in first 3 tool calls. Guarantees output even if you run out of turns.
- **Budget turns for output.** Reserve 3+ calls for writing. 70% used + no output = STOP and write.
- **Name every test function in the spec.** "Add 3 tests" loops. Use exact names + "Stop after these N tests."
- **Max 1 commit for test specs.** If tests don't pass first try, stop and report.
- **Report:** `DONE`, `DONE_WITH_CONCERNS`, `NEEDS_CONTEXT`, or `BLOCKED`.

## Gotchas

- `scope:` in spec frontmatter can be a bare string (`scope: mtor`) or list — `_inject_spec_constraints` handles both.
- Dedup uses prompt text + spec path hash. Same prompt + different spec = different key. Same prompt + same spec within 5 min = blocked.
- `_auto_commit()` in translocase runs after successful ribosome exit — safety net for GLM forgetting to commit.
- Verdict gate flags: `no_commit_on_success`, `destruction`, `target_file_missing`, `nested_test_file`. Any flag → rejected unless scout mode.
