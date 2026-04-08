"""mtor — agent-first coding task dispatcher for Temporal workflows."""

from __future__ import annotations

import os

VERSION = "0.7.0"
TEMPORAL_HOST = os.environ.get("MTOR_TEMPORAL_HOST", "localhost:7233")
TASK_QUEUE = os.environ.get("MTOR_TASK_QUEUE", "translation-queue")
WORKFLOW_TYPE = os.environ.get("MTOR_WORKFLOW_TYPE", "TranslationWorkflow")
WORKER_HOST = os.environ.get("MTOR_WORKER_HOST", "localhost")
DEPLOY_REMOTE = os.environ.get("MTOR_DEPLOY_REMOTE", "ganglion")
# Avoid pathlib at module level — Temporal workflow sandbox restricts Path.home()/Path.cwd()
_HOME = os.environ.get("HOME", "/home/vivesca")
REPO_DIR = os.environ.get("MTOR_REPO_DIR", os.path.join(_HOME, "germline"))
OUTPUTS_DIR = os.environ.get("MTOR_OUTPUTS_DIR", os.path.join(_HOME, ".mtor", "outputs"))
LOG_TAIL_LINES = 30

# Optional coaching file path (string, not pathlib — Temporal sandbox restriction)
COACHING_PATH: str | None = os.environ.get("MTOR_COACHING_PATH") or None
COACHING_MAX_KB = 10  # Hard cap — coaching + spec must fit under 15KB or GLM exits immediately

__version__ = VERSION
