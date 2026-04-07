#!/usr/bin/env python3
"""Shared data models for polysome.

Extracted to avoid circular imports between translocase.py and workflow.py.
"""

from dataclasses import dataclass, field

# ── Activity result ──────────────────────────────────────────────────


@dataclass
class TranslationResult:
    """Outcome of a single translation invocation."""

    provider: str
    task: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def __str__(self) -> str:
        status = "OK" if self.ok else "FAIL"
        return f"[{status}] provider={self.provider} exit={self.exit_code} task={self.task!r}"


# ── Workflow input / output ──────────────────────────────────────────


@dataclass
class TranslationTaskSpec:
    """A single task to dispatch."""

    provider: str
    task: str


@dataclass
class TranslationBatchInput:
    """Workflow input: a batch of translation tasks."""

    tasks: list[TranslationTaskSpec] = field(default_factory=list)


@dataclass
class TranslationBatchOutput:
    """Workflow output: results keyed by provider."""

    results: list[TranslationResult] = field(default_factory=list)
    total: int = 0
    succeeded: int = 0
    failed: int = 0

    def __str__(self) -> str:
        lines = [
            f"TranslationBatch: {self.succeeded}/{self.total} succeeded, {self.failed} failed",
        ]
        for r in self.results:
            marker = "OK" if r.ok else "FAIL"
            lines.append(f"  [{marker}] {r.provider}: {r.task!r}")
        return "\n".join(lines)
