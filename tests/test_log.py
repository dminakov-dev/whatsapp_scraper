import logging

import pytest

from collector.log import _MARKER, setup_logging


@pytest.fixture(autouse=True)
def restore_root_logger():
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    root.handlers = [h for h in root.handlers if not getattr(h, _MARKER, False)]
    yield
    for h in root.handlers:
        if getattr(h, _MARKER, False):
            h.close()
    root.handlers, root.level = saved_handlers, saved_level


def test_writes_to_per_process_file_including_submodule_loggers(tmp_path):
    log = setup_logging("worker", str(tmp_path / "logs"))
    log.info("hello from entrypoint")
    logging.getLogger("collector.db").warning("hello from module")
    for h in logging.getLogger().handlers:
        h.flush()
    text = (tmp_path / "logs" / "worker.log").read_text()
    assert "hello from entrypoint" in text and "collector.db: hello from module" in text


def test_setup_is_idempotent(tmp_path):
    setup_logging("worker", str(tmp_path))
    setup_logging("worker", str(tmp_path))
    ours = [h for h in logging.getLogger().handlers if getattr(h, _MARKER, False)]
    assert len(ours) == 2                      # one file handler + one stdout handler


def test_file_handler_rotates(tmp_path):
    setup_logging("worker", str(tmp_path))
    file_handler = next(h for h in logging.getLogger().handlers
                        if getattr(h, _MARKER, False) and hasattr(h, "maxBytes"))
    assert file_handler.maxBytes == 5 * 1024 * 1024 and file_handler.backupCount == 5


def test_level_and_httpx_noise(tmp_path):
    setup_logging("worker", str(tmp_path), level="debug")
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("httpx").level == logging.WARNING
