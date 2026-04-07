"""mtor — agent-first coding task dispatcher for Temporal workflows."""

from __future__ import annotations

import os
from pathlib import Path

VERSION = "0.5.0"
TEMPORAL_HOST = os.environ.get("MTOR_TEMPORAL_HOST", "localhost:7233")
TASK_QUEUE = os.environ.get("MTOR_TASK_QUEUE", "translation-queue")
WORKFLOW_TYPE = os.environ.get("MTOR_WORKFLOW_TYPE", "TranslationWorkflow")
WORKER_HOST = os.environ.get("MTOR_WORKER_HOST", "localhost")
DEPLOY_REMOTE = os.environ.get("MTOR_DEPLOY_REMOTE", "ganglion")
REPO_DIR = os.environ.get("MTOR_REPO_DIR", str(Path.cwd()))
OUTPUTS_DIR = os.environ.get("MTOR_OUTPUTS_DIR", str(Path.home() / ".mtor" / "outputs"))
LOG_TAIL_LINES = 30

# Optional coaching file — empty string means disabled
_coaching = os.environ.get("MTOR_COACHING_PATH", "")
COACHING_PATH = Path(_coaching) if _coaching else None

__version__ = VERSION
