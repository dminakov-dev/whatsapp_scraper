import argparse
import json

import pytest

from collector import db
from collector.export import EXPORT_FIELDS, export_jsonl, positive_int
from tests.conftest import make_row


def seed(conn):
    db.insert_messages(conn, [
        make_row(message_id="g2", date="2026-10-17T10:05:00Z", text="Привіт, світ"),
        make_row(message_id="g1", date="2026-10-17T10:00:00Z"),
        make_row(chat_id="1@newsletter", chat_type="channel", message_id="c1",
                 date="2026-10-17T09:00:00Z", url="https://whatsapp.com/channel/X/1"),
    ])


def read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_export_writes_valid_jsonl_oldest_first(conn, tmp_path):
    seed(conn)
    out = tmp_path / "out" / "m.jsonl"
    assert export_jsonl(conn, str(out)) == 3
    records = read(out)
    assert [r["message_id"] for r in records] == ["c1", "g1", "g2"]
    assert list(records[0]) == list(EXPORT_FIELDS)            # canonical core first
    assert {"author", "url", "date", "text"} <= set(records[0])


def test_export_keeps_non_ascii_readable(conn, tmp_path):
    seed(conn)
    out = tmp_path / "m.jsonl"
    export_jsonl(conn, str(out))
    assert "Привіт, світ" in out.read_text(encoding="utf-8")


def test_export_filters(conn, tmp_path):
    seed(conn)
    out = tmp_path / "m.jsonl"
    assert export_jsonl(conn, str(out), chat_type="channel") == 1
    assert export_jsonl(conn, str(out), since="2026-10-17T10:00:00Z") == 2
    assert export_jsonl(conn, str(out), limit=1) == 1


def test_export_replaces_file_atomically(conn, tmp_path):
    seed(conn)
    out = tmp_path / "m.jsonl"
    out.write_text("old content\n")
    export_jsonl(conn, str(out))
    assert len(read(out)) == 3
    assert not (tmp_path / "m.jsonl.tmp").exists()


# --- review fix 6: --limit must be positive ------------------------------------------------

@pytest.mark.parametrize("limit", [0, -1])
def test_export_rejects_non_positive_limit(conn, tmp_path, limit):
    seed(conn)
    with pytest.raises(ValueError, match="limit must be at least 1"):
        export_jsonl(conn, str(tmp_path / "m.jsonl"), limit=limit)


@pytest.mark.parametrize("value", ["0", "-5", "abc"])
def test_cli_limit_type_rejects_non_positive(value):
    with pytest.raises(argparse.ArgumentTypeError):
        positive_int(value)
    assert positive_int("3") == 3
