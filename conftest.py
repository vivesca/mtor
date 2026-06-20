from __future__ import annotations

import pytest

from mtor.worker import chaperone_review


@pytest.fixture(autouse=True)
def _isolate_chaperone_review_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        chaperone_review,
        "REVIEW_LOG",
        tmp_path / "ribosome-reviews.jsonl",
    )
    monkeypatch.setattr(
        chaperone_review,
        "DOSSIER_DIR",
        tmp_path / "ribosome-dossiers",
    )
