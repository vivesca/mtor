"""Coaching loader — reads provider-specific coaching notes for ribosome tasks.

Coaching notes are injected into task prompts to guide LLM behavior (e.g.
known failure patterns, code style rules).  Each provider may have different
formatting requirements or may opt out entirely.
"""

from __future__ import annotations

import sys
from pathlib import Path

from mtor import COACHING_MAX_KB

# Providers that do not receive coaching (they have their own instruction sets).
_NO_COACHING_PROVIDERS: frozenset[str] = frozenset({"goose", "droid"})


def load_coaching(
    path: str | Path | None,
    provider: str,
    max_kb: int = COACHING_MAX_KB,
) -> str | None:
    """Load coaching notes from *path* for *provider*.

    Returns the coaching text string, or ``None`` when:
      - *path* is ``None`` or empty
      - the file does not exist
      - the provider is in ``_NO_COACHING_PROVIDERS`` (e.g. goose, droid)
      - the file exceeds *max_kb*

    For the ``codex`` provider the content is wrapped in a
    ``<system-reminder>`` envelope.
    """
    # Providers with built-in instruction sets — skip external coaching
    if provider in _NO_COACHING_PROVIDERS:
        return None

    if not path:
        return None

    file_path = Path(path) if isinstance(path, str) else path
    if not file_path.exists():
        return None

    content = file_path.read_text(encoding="utf-8")
    size_kb = len(content.encode("utf-8")) / 1024

    if size_kb > max_kb:
        print(
            f"[coaching] file {file_path} is {size_kb:.1f}KB, "
            f"exceeds {max_kb}KB budget — skipping",
            file=sys.stderr,
        )
        return None

    # Codex variant: wrap in codex-specific XML envelope
    if provider == "codex":
        return f"<system-reminder>\n{content}\n</system-reminder>"

    return content
