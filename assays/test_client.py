"""Tests for client.py — Temporal client connection helper."""
from unittest.mock import patch, MagicMock, AsyncMock

from mtor.client import _get_client


def test_success_returns_client_and_none() -> None:
    """Successful connection returns (client, None)."""
    fake_client = MagicMock(name="TemporalClient")
    mock_client_cls = MagicMock(name="Client")
    mock_client_cls.connect = AsyncMock(return_value=fake_client)

    with patch.dict("sys.modules", {"temporalio": MagicMock(), "temporalio.client": MagicMock(Client=mock_client_cls)}):
        result_client, result_err = _get_client()

    assert result_client is fake_client
    assert result_err is None


def test_import_error_returns_sdk_message() -> None:
    """If temporalio is missing, returns (None, 'temporalio SDK not installed')."""
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _block_temporalio(name, *args, **kwargs):
        if name == "temporalio" or name.startswith("temporalio."):
            raise ImportError("no temporalio")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_block_temporalio):
        result_client, result_err = _get_client()

    assert result_client is None
    assert result_err == "temporalio SDK not installed"


def test_connection_error_returns_exception_message() -> None:
    """If Client.connect raises, returns (None, str(exc))."""
    mock_client_cls = MagicMock(name="Client")
    mock_client_cls.connect = AsyncMock(side_effect=RuntimeError("connection refused"))

    with patch.dict("sys.modules", {"temporalio": MagicMock(), "temporalio.client": MagicMock(Client=mock_client_cls)}):
        result_client, result_err = _get_client()

    assert result_client is None
    assert result_err == "connection refused"
