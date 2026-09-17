import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from pulse_sdk import PulseClient


def test_capture_requires_a_user_or_anonymous_id() -> None:
    client = PulseClient("pulse_write_x", "http://api.test")
    with pytest.raises(ValueError):
        client.capture("button clicked")


def test_capture_returns_a_unique_event_id_and_does_not_flush_below_threshold() -> None:
    client = PulseClient("pulse_write_x", "http://api.test", flush_at_size=10)
    with patch("urllib.request.urlopen") as urlopen:
        first = client.capture("a", user_id="u1")
        second = client.capture("b", user_id="u1")

    assert first != second
    urlopen.assert_not_called()


def test_capture_auto_flushes_once_the_buffer_reaches_flush_at_size() -> None:
    client = PulseClient("pulse_write_x", "http://api.test", flush_at_size=2)
    with patch("urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value = MagicMock(status=202)
        client.capture("a", user_id="u1")
        client.capture("b", user_id="u1")

    urlopen.assert_called_once()


def test_flush_sends_the_batch_with_the_write_key_header() -> None:
    client = PulseClient("pulse_write_x", "http://api.test", flush_at_size=100)
    client.capture("checkout completed", user_id="u1", properties={"revenue": 9.99})

    with patch("urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value = MagicMock(status=202)
        ok = client.flush()

    assert ok is True
    request = urlopen.call_args[0][0]
    assert request.full_url == "http://api.test/ingest"
    # urllib.request.Request normalizes header names via str.capitalize() on
    # both insert and lookup, so "X-API-Key" is actually stored as
    # "X-api-key" -- get_header() itself does no further normalization.
    assert request.get_header("X-api-key") == "pulse_write_x"
    body = json.loads(request.data)
    assert body["batch"][0]["event"] == "checkout completed"
    assert body["batch"][0]["properties"] == {"revenue": 9.99}


def test_flush_leaves_the_buffer_intact_on_failure_and_never_regenerates_event_id() -> None:
    client = PulseClient("pulse_write_x", "http://api.test", flush_at_size=100)
    event_id = client.capture("a", user_id="u1")

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
        ok = client.flush()
    assert ok is False

    with patch("urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value = MagicMock(status=202)
        ok_retry = client.flush()

    assert ok_retry is True
    sent_id = json.loads(urlopen.call_args[0][0].data)["batch"][0]["event_id"]
    assert sent_id == event_id


def test_flush_is_a_noop_with_an_empty_buffer() -> None:
    client = PulseClient("pulse_write_x", "http://api.test")
    with patch("urllib.request.urlopen") as urlopen:
        assert client.flush() is True
    urlopen.assert_not_called()


def test_context_manager_flushes_on_exit() -> None:
    with patch("urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value = MagicMock(status=202)
        with PulseClient("pulse_write_x", "http://api.test") as client:
            client.capture("a", user_id="u1")
        urlopen.assert_called_once()
