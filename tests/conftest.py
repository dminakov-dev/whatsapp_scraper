import pytest

from collector import db


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def make_row(chat_id="123@g.us", message_id="m1", date="2026-10-17T10:00:00Z", **overrides):
    """A valid canonical message row; override any field per test."""
    row = {
        "source": "whatsapp",
        "chat_id": chat_id,
        "chat_type": "group",
        "message_id": message_id,
        "author": "111@lid",
        "url": f"wa://{chat_id}/{message_id}",
        "date": date,
        "text": "hello",
        "media_type": None,
        "collected_at": "2026-10-17T12:00:00Z",
        "via": "backfill",
    }
    row.update(overrides)
    return row
