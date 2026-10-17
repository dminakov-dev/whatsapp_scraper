from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge_mock.app import create_app
from bridge_mock.settings import Faults, MockSettings
from bridge_mock.world import World
from collector import db
from collector.source import (AccountError, BridgeClient, RetryableError, TargetError,
                              classify, parse_retry_after)
from collector.timeutil import iso_from_unix

NOW = 1_760_700_000
GROUP = "120363111111111111@g.us"


# --- pure classification ----------------------------------------------------------------

@pytest.mark.parametrize("status, error_type, kind", [
    (200, None, None),
    (401, AccountError, "auth_failure"),
    (429, AccountError, "rate_limited"),
    (409, RetryableError, None),
    (500, RetryableError, None),
    (503, RetryableError, None),
    (400, TargetError, None),
    (403, TargetError, None),
    (404, TargetError, None),
    (410, TargetError, None),
])
def test_classify(status, error_type, kind):
    error = classify(status, {}, "x")
    if error_type is None:
        assert error is None
    else:
        assert type(error) is error_type
        if kind:
            assert error.kind == kind


def test_429_never_becomes_a_permanent_skip():
    assert not isinstance(classify(429, {"Retry-After": "5"}, "x"), TargetError)


def test_parse_retry_after_seconds_and_http_date():
    assert parse_retry_after("30") == 30.0
    future = datetime.now(timezone.utc) + timedelta(seconds=120)
    assert 100 < parse_retry_after(format_datetime(future, usegmt=True)) <= 120
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None


# --- transport-level failures (fake transport) ------------------------------------------

def client_raising(exc_type):
    def handler(request):
        raise exc_type("boom", request=request)
    return BridgeClient("http://bridge", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_unreachable_bridge_is_an_account_error():
    with pytest.raises(AccountError) as err:
        client_raising(httpx.ConnectError).health()
    assert err.value.kind == "disconnected"


def test_read_timeout_is_retryable_for_the_target():
    with pytest.raises(RetryableError):
        client_raising(httpx.ReadTimeout).fetch_history(GROUP, None, 10)


def test_invalid_json_is_retryable():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>oops</html>"))
    client = BridgeClient("http://bridge", client=httpx.Client(transport=transport))
    with pytest.raises(RetryableError):
        client.fetch_history(GROUP, None, 10)


def test_messages_from_another_chat_are_dropped():
    foreign = {"id": "x", "chat_id": "other@g.us", "author": "1@lid", "timestamp": NOW,
               "body": "hi", "type": "chat"}
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"messages": [foreign], "oldest_available": NOW}))
    client = BridgeClient("http://bridge", client=httpx.Client(transport=transport))
    page = client.fetch_history(GROUP, None, 10)
    assert page.messages == [] and page.dropped == 1


# --- against the mock bridge app --------------------------------------------------------

def mock_client(conn=None, **faults):
    settings = MockSettings(emit_interval=0, faults=Faults(**faults))
    app = create_app(settings, World(seed=1, now=NOW))
    recorder = (lambda *a, **k: db.log_event(conn, *a, **k)) if conn else None
    return BridgeClient("http://testserver", client=TestClient(app), recorder=recorder)


def test_list_chats_derives_types():
    chats = {c["chat_id"]: c["chat_type"] for c in mock_client().list_chats()}
    assert chats[GROUP] == "group"
    assert chats["120363000000000001@newsletter"] == "channel"
    assert chats["380500000001@c.us"] == "direct"


def test_fetch_history_returns_canonical_messages_and_inclusive_since():
    client = mock_client()
    first = client.fetch_history(GROUP, None, 10)
    assert len(first.messages) == 10 and first.dropped == 0
    assert all(m.via == "backfill" and m.chat_id == GROUP for m in first.messages)
    assert first.newest == max(m.date for m in first.messages)

    again = client.fetch_history(GROUP, first.newest, 10)
    assert again.messages[0].date == first.newest  # inclusive: boundary second re-read


def test_oldest_available_is_reported():
    page = mock_client().fetch_history(GROUP, iso_from_unix(0), 5)
    assert page.oldest_available == page.messages[0].date


@pytest.mark.parametrize("faults, error_type", [
    ({"fail_5xx": 1.0}, RetryableError),
    ({"fail_409": 1.0}, RetryableError),
    ({"fail_429": 1.0}, AccountError),
    ({"auth_fail": True}, AccountError),
])
def test_mock_faults_map_to_error_types(faults, error_type):
    with pytest.raises(error_type):
        mock_client(**faults).fetch_history(GROUP, None, 10)


def test_429_carries_retry_after():
    with pytest.raises(AccountError) as err:
        mock_client(fail_429=1.0, retry_after=12).fetch_history(GROUP, None, 10)
    assert err.value.retry_after == 12.0 and err.value.kind == "rate_limited"


def test_dead_chat_is_a_target_error():
    with pytest.raises(TargetError) as err:
        mock_client().fetch_history("120363999999999999@g.us", None, 10)
    assert err.value.status_code == 404


def test_health_fails_when_unpaired():
    with pytest.raises(AccountError) as err:
        mock_client(auth_fail=True).health()
    assert err.value.kind == "auth_failure"


def test_every_response_is_recorded_as_an_account_event(conn):
    client = mock_client(conn)
    client.fetch_history(GROUP, None, 5)
    with pytest.raises(AccountError):
        mock_client(conn, fail_429=1.0, retry_after=9).fetch_history(GROUP, None, 5)
    rows = conn.execute("SELECT kind, status_code, latency_ms, detail FROM account_events "
                        "ORDER BY id").fetchall()
    assert [(r["kind"], r["status_code"]) for r in rows] == [("request", 200), ("rate_limited", 429)]
    assert rows[0]["latency_ms"] is not None
    assert "retry-after=9" in rows[1]["detail"].lower()
