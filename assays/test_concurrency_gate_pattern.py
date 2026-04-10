"""Tests for the translocase concurrency gate pgrep pattern.

The gate must match only ribosome effector invocations, not any process
containing "ribosome" in its command line (e.g. rsync of ribosome-outputs/).

Runs via: cd ~/code/mtor && uv run pytest assays/test_concurrency_gate_pattern.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_gate_pattern_matches_only_ribosome_provider_invocations():
    """The pgrep pattern in translocase.translate must include "--provider"."""
    src = Path(__file__).resolve().parents[1] / "mtor" / "worker" / "translocase.py"
    text = src.read_text()
    # The fix replaces bare "ribosome" with "ribosome --provider"
    assert '"ribosome --provider"' in text, (
        "concurrency gate pgrep pattern must be 'ribosome --provider', not bare 'ribosome'"
    )
    # Defensive: the bare pattern should not appear in a pgrep arg
    assert '"-cf", "ribosome"' not in text, (
        "bare 'ribosome' pgrep pattern is back — it false-matches rsync of ribosome-outputs/"
    )


def test_gate_does_not_count_rsync_outputs():
    """Simulated pgrep output: 1 effector + 6 rsync processes → count == 1."""
    # The fixed pattern is "ribosome --provider", so pgrep -cf would only match
    # processes whose command line contains that exact substring.
    fake_processes = [
        "bash /home/vivesca/germline/effectors/ribosome --provider zhipu Fix something",
        "/bin/sh -c rsync -a ~/germline/loci/ribosome-outputs/ soma:germline/loci/ribosome-outputs/",
        "rsync -a /home/vivesca/germline/loci/ribosome-outputs/ soma:germline/loci/ribosome-outputs/",
        "ssh soma rsync --server -logDtpre.iLsfxCIvu . germline/loci/ribosome-outputs/",
        "/bin/sh -c rsync -a ~/germline/loci/ribosome-outputs/ soma:germline/loci/ribosome-outputs/",
        "rsync -a /home/vivesca/germline/loci/ribosome-outputs/ soma:germline/loci/ribosome-outputs/",
        "ssh soma rsync --server -logDtpre.iLsfxCIvu . germline/loci/ribosome-outputs/",
    ]
    pattern = "ribosome --provider"
    matched = [p for p in fake_processes if pattern in p]
    assert len(matched) == 1, (
        f"pattern {pattern!r} should match exactly 1 process, matched {len(matched)}"
    )


def test_gate_counts_multiple_concurrent_effectors():
    """Multiple concurrent ribosome effectors should all be counted."""
    fake_processes = [
        "bash /home/vivesca/germline/effectors/ribosome --provider zhipu task A",
        "bash /home/vivesca/germline/effectors/ribosome --provider volcano task B",
        "bash /home/vivesca/germline/effectors/ribosome --provider infini task C",
    ]
    pattern = "ribosome --provider"
    matched = [p for p in fake_processes if pattern in p]
    assert len(matched) == 3
