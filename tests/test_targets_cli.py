import io

import pytest

from collector import db
from collector.targets import run

GROUP = "a@g.us"
DEAD = "dead@g.us"
CHANNEL = "c@newsletter"


@pytest.fixture
def seeded(conn):
    db.sync_targets(conn, [{"chat_id": c, "chat_type": t, "name": c}
                           for c, t in ((GROUP, "group"), (DEAD, "group"), (CHANNEL, "channel"))])
    db.insert_and_advance(conn, DEAD, [], "2026-10-17T10:00:00Z", None)
    db.mark_retry(conn, DEAD, "503: flaky", "2026-10-17T11:00:00Z")
    db.mark_skipped(conn, DEAD, "404: chat not found")
    return conn


def cli(conn, *argv):
    out = io.StringIO()
    code = run(list(argv), conn, out)
    return code, out.getvalue()


def test_list_shows_all_targets(seeded):
    code, out = cli(seeded, "list")
    assert code == 0
    assert all(c in out for c in (GROUP, DEAD, CHANNEL))


def test_list_skipped_shows_only_skipped_with_reason(seeded):
    code, out = cli(seeded, "list", "--skipped")
    assert code == 0
    assert DEAD in out and "404: chat not found" in out
    assert GROUP not in out and CHANNEL not in out


def test_list_on_empty_db(conn):
    assert cli(conn, "list")[1].strip() == "no targets"


def test_reenable_one_keeps_cursor_and_clears_failure_state(seeded):
    code, out = cli(seeded, "reenable", DEAD)
    assert code == 0 and "re-enabled" in out
    t = db.get_target(seeded, DEAD)
    assert (t["status"], t["enabled"], t["fail_count"], t["last_error"], t["next_run_at"]) == \
        ("active", 1, 0, None, None)
    assert t["cursor_ts"] == "2026-10-17T10:00:00Z"
    assert seeded.execute("SELECT COUNT(*) FROM account_events "
                          "WHERE kind='target_reenabled' AND chat_id=?", (DEAD,)).fetchone()[0] == 1


def test_reenable_active_target_is_a_no_op(seeded):
    code, out = cli(seeded, "reenable", GROUP)
    assert code == 0 and "already active" in out


def test_reenable_unknown_target_fails(seeded):
    code, out = cli(seeded, "reenable", "nope@g.us")
    assert code == 1 and "unknown target" in out


def test_disable_then_reenable_all(seeded):
    assert cli(seeded, "disable", CHANNEL)[0] == 0
    assert db.get_target(seeded, CHANNEL)["enabled"] == 0
    assert db.pick_due_target(seeded, "2030-01-01T00:00:00Z")["chat_id"] == GROUP

    code, out = cli(seeded, "reenable", "--all")
    assert code == 0 and "re-enabled 2 target(s)" in out      # DEAD (skipped) + CHANNEL (off)
    assert all(t["status"] == "active" and t["enabled"] for t in db.list_targets(seeded))


def test_disable_unknown_target_fails(seeded):
    assert cli(seeded, "disable", "nope@g.us")[0] == 1


@pytest.mark.parametrize("argv", [[], ["reenable"], ["reenable", DEAD, "--all"], ["frobnicate"]])
def test_bad_arguments_exit_with_usage_error(seeded, argv):
    with pytest.raises(SystemExit) as err:
        cli(seeded, *argv)
    assert err.value.code == 2
