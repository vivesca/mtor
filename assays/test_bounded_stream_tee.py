"""Regression tests for bounded subprocess capture and workflow logging."""

from __future__ import annotations

import asyncio
import io

from mtor.worker.translocase import _tee_stream


def _reader(*chunks: bytes) -> asyncio.StreamReader:
    stream = asyncio.StreamReader()
    for chunk in chunks:
        stream.feed_data(chunk)
    stream.feed_eof()
    return stream


def test_tee_stream_preserves_small_output_and_counts_all_bytes():
    async def run():
        counter = [0]
        log_counter = [0]
        log_truncated = [False]
        log = io.BytesIO()
        result = await _tee_stream(
            _reader(b"hello", b" world"),
            log,
            "stdout",
            counter,
            capture_limit=64,
            log_counter=log_counter,
            log_limit=64,
            log_truncated=log_truncated,
        )
        return result, counter, log_counter, log_truncated, log.getvalue()

    result, counter, log_counter, log_truncated, logged = asyncio.run(run())

    assert result == b"hello world"
    assert counter == [11]
    assert log_counter == [20]
    assert log_truncated == [False]
    assert logged == b"[stdout] hello world"


def test_tee_stream_keeps_bounded_tail_but_drains_and_counts_every_byte():
    async def run():
        counter = [0]
        return await _tee_stream(
            _reader(b"abcdefgh", b"ijklmnop", b"qrstuvwx"),
            None,
            "stdout",
            counter,
            capture_limit=16,
        ), counter

    result, counter = asyncio.run(run())

    assert len(result) == 16
    assert b"truncated" in result
    assert result.endswith(b"uvwx")
    assert counter == [24]


def test_tee_stream_enforces_shared_log_budget_and_marks_truncation_once():
    async def run():
        log = io.BytesIO()
        log_counter = [0]
        log_truncated = [False]
        await _tee_stream(
            _reader(b"a" * 20),
            log,
            "stdout",
            capture_limit=64,
            log_counter=log_counter,
            log_limit=12,
            log_truncated=log_truncated,
        )
        await _tee_stream(
            _reader(b"b" * 20),
            log,
            "stderr",
            capture_limit=64,
            log_counter=log_counter,
            log_limit=12,
            log_truncated=log_truncated,
        )
        return log.getvalue(), log_counter, log_truncated

    logged, log_counter, log_truncated = asyncio.run(run())

    assert logged[:12] == b"[stdout] aaa"
    assert logged.count(b"mtor log truncated") == 1
    assert log_counter == [12]
    assert log_truncated == [True]
