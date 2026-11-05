"""运行日志：文件初始化与用户操作访问日志。"""
from __future__ import annotations

import logging

import pytest

from anvil import logging_setup
from tests.conftest import API

pytestmark = pytest.mark.usefixtures("client")


def test_sanitize_proxy_hides_credentials():
    assert logging_setup.sanitize_proxy(
        "http://user:pass@proxy.local:8080/path") == \
        "http://***@proxy.local:8080/path"
    assert logging_setup.sanitize_proxy("http://proxy.local:8080") == \
        "http://proxy.local:8080"


def test_setup_logging_writes_utf8_file(tmp_path):
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    old_uvicorn = {
        name: list(logging.getLogger(name).handlers)
        for name in logging_setup._UVICORN_LOGGERS
    }
    old_handler = logging_setup._handler
    old_path = logging_setup._log_path
    try:
        path = logging_setup.setup_logging(tmp_path)
        logging.getLogger("anvil.test").info("hello-日志")
        assert path == tmp_path / "anvil.log"
        assert path.exists()
        assert "hello-日志" in path.read_text(encoding="utf-8")
    finally:
        for handler in list(root.handlers):
            if handler not in old_handlers:
                root.removeHandler(handler)
                handler.close()
        for name, handlers in old_uvicorn.items():
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                if handler not in handlers:
                    logger.removeHandler(handler)
        root.setLevel(old_level)
        logging_setup._handler = old_handler
        logging_setup._log_path = old_path


async def test_api_requests_are_logged(client, caplog):
    caplog.set_level(logging.INFO, logger="anvil.access")
    r = await client.get(f"{API}/tasks")
    assert r.status_code == 200
    assert "http id=" in caplog.text
    assert f"path={API}/tasks" in caplog.text
    assert "status=200" in caplog.text
