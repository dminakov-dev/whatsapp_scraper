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
    (400, AccountError, "bad_request"),
    (403, TargetError, None),
    (404, TargetError, None),
    (410, TargetError, None),
    # review finding 4: only 403/404/410 skip a chat
    (408, RetryableError, None),           # request timeout: transient
    (405, AccountError, "bad_request"),    # unexpected 4xx = our bug -> pause the loop
    (413, AccountError, "bad_request"),
    (422, AccountError, "bad_request"),
    (418, AccountError, "bad_request"),
])
def test_classify(status, error_type, kind):
    error = classify(status, {}, "x")
    if error_type is None:
        assert error is None
    else:
        assert type(error) is error_type
        if kind:
            assert error.kind == kind


@pytest.mark.parametrize("status", [400, 405, 408, 409, 413, 422, 429, 500])
def test_never_a_permanent_skip(status):
    assert not isinstance(classify(status, {"Retry-After": "5"}, "x"), TargetError)


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


# --- coverage gaps: unusual bridge answers ----------------------------------------------

def client_answering(*, json=None, raise_exc=None, events=None):
    def handler(request):
        if raise_exc:
            raise raise_exc("broken", request=request)
        return httpx.Response(200, json=json)

    recorder = (lambda kind, *a, **k: events.append(kind)) if events is not None else None
    return BridgeClient("http://bridge", recorder=recorder,
                        client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_other_transport_errors_are_retryable_and_recorded():
    events = []
    with pytest.raises(RetryableError):
        client_answering(raise_exc=httpx.RemoteProtocolError, events=events).fetch_history(GROUP, None, 5)
    assert events == ["bridge_error"]


def test_non_object_body_is_retryable():
    with pytest.raises(RetryableError):
        client_answering(json=[1, 2, 3]).fetch_history(GROUP, None, 5)


def test_history_without_messages_list_is_retryable():
    with pytest.raises(RetryableError):
        client_answering(json={"messages": "nope"}).fetch_history(GROUP, None, 5)


def test_health_200_but_not_connected_is_an_account_error():
    with pytest.raises(AccountError) as err:
        client_answering(json={"ok": False, "state": "OPENING"}).health()
    assert err.value.kind == "auth_failure" and "OPENING" in err.value.detail


def test_malformed_chat_entries_are_skipped():
    chats = client_answering(json={"chats": [
        {"chat_id": GROUP, "name": "ok"},
        {"name": "no id"},
        "not a dict",
        {"chat_id": "weird-jid"},
    ]}).list_chats()
    assert [c["chat_id"] for c in chats] == [GROUP]


def test_close_closes_the_http_client():
    client = client_answering(json={})
    client.close()
    assert client.http.is_closed


# --- review finding 7: /health failures are loop-level -------------------------------------

def health_client(handler, events):
    return BridgeClient("http://bridge", recorder=lambda kind, *a, **k: events.append(kind),
                        client=httpx.Client(transport=httpx.MockTransport(handler)))


def raise_read_timeout(request):
    raise httpx.ReadTimeout("slow", request=request)


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(503, json={"error": "loading"}),
    lambda r: httpx.Response(409, json={"error": "busy"}),
    lambda r: httpx.Response(404, json={"error": "no such endpoint"}),
    lambda r: httpx.Response(200, text="<html>not json</html>"),
    raise_read_timeout,
], ids=["503", "409", "404", "bad-json", "timeout"])
def test_failing_health_check_is_an_account_error(handler):
    events = []
    with pytest.raises(AccountError) as err:
        health_client(handler, events).health()
    assert err.value.kind == "bridge_unhealthy"
    assert events[-1] == "bridge_unhealthy"


# --- review findings 1 and 2: the page position comes from the raw page --------------------

def history_client(messages):
    def handler(request):
        return httpx.Response(200, json={"messages": messages, "oldest_available": None})
    return BridgeClient("http://bridge", client=httpx.Client(transport=httpx.MockTransport(handler)))


def raw(i, ts, **overrides):
    msg = {"id": f"m{i}", "chat_id": GROUP, "author": "111@lid", "timestamp": ts,
           "body": "hi", "type": "chat"}
    msg.update(overrides)
    return msg


def test_page_position_includes_dropped_messages_but_not_unreadable_ones():
    page = history_client([
        raw(1, NOW),
        raw(2, NOW + 5, author=None),          # malformed (group without author), readable time
        raw(3, "garbage"),                     # unreadable time: no position at all
    ]).fetch_history(GROUP, None, 10)
    assert [m.message_id for m in page.messages] == ["m1"]
    assert page.dropped == 2 and page.future == 0
    assert page.newest == iso_from_unix(NOW + 5)


def test_future_dated_messages_are_counted_and_never_become_the_position():
    import time
    page = history_client([raw(1, NOW), raw(2, int(time.time()) + 86400)]).fetch_history(GROUP, None, 10)
    assert [m.message_id for m in page.messages] == ["m1"]
    assert (page.dropped, page.future) == (1, 1)
    assert page.newest == iso_from_unix(NOW)


def test_messages_of_another_chat_never_become_the_position():
    page = history_client([raw(1, NOW), raw(2, NOW + 99, chat_id="other@g.us")]).fetch_history(
        GROUP, None, 10)
    assert page.newest == iso_from_unix(NOW) and page.dropped == 1
