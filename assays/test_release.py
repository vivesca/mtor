"""Assays for the mtor release workflow.

Spec: ~/epigenome/chromatin/loci/plans/mtor-pypi-release-workflow.md
"""
from __future__ import annotations

from pathlib import Path


def test_release_refuses_dirty_repo():
    """Sentinel: implement `mtor release` dirty-repo guard."""
    assert False, "mtor release dirty-repo guard is not implemented yet"


def test_release_bumps_version(tmp_path: Path):
    """Sentinel: implement version bump, tag, build, and publish orchestration."""
    assert False, "mtor release version bump workflow is not implemented yet"
