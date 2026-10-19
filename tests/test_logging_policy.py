"""Policy test: plugin logging must go through ``astrbot.api.logger``.

AstrBot plugins may not use the stdlib ``logging`` module; ``main.py`` injects the
``astrbot.api`` logger into the ``chess_site`` session/protocol layers, and the
package falls back to a silent sink only for standalone/test usage.
"""

from __future__ import annotations

from pathlib import Path

from chess_site import logs, protocol, session

ROOT = Path(__file__).resolve().parents[1]


def _source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_no_stdlib_logging_in_package():
    for name in ("chess_site/protocol.py", "chess_site/session.py", "chess_site/logs.py"):
        source = _source(name)
        assert "import logging" not in source, name
        assert "logging." not in source, name
        assert "getLogger" not in source, name
        assert "LOGGER =" not in source, name


def test_main_uses_astrbot_logger_and_injects_it():
    source = _source("main.py")
    assert "from astrbot.api import logger" in source
    # the injected logger is passed to every room session
    assert "logger=logger" in source


def test_session_forwards_injected_logger_to_connection():
    source = _source("chess_site/session.py")
    assert source.count("logger=self.logger") >= 2  # initial connect + reconnect


def test_silent_fallback_is_noop():
    sink = logs.SilentLog()
    for method in ("debug", "info", "warning", "error", "exception", "critical"):
        getattr(sink, method)("message %s", 1, exc_info=True)


class _FakeLog:
    def __init__(self) -> None:
        self.records: list[tuple[str, tuple]] = []

    def _record(self, level: str, args: tuple, kwargs: dict) -> None:
        self.records.append((level, args))

    def debug(self, *args, **kwargs) -> None:
        self._record("debug", args, kwargs)

    def info(self, *args, **kwargs) -> None:
        self._record("info", args, kwargs)

    def warning(self, *args, **kwargs) -> None:
        self._record("warning", args, kwargs)

    def error(self, *args, **kwargs) -> None:
        self._record("error", args, kwargs)

    def exception(self, *args, **kwargs) -> None:
        self._record("exception", args, kwargs)

    def critical(self, *args, **kwargs) -> None:
        self._record("critical", args, kwargs)


def test_logger_injection_protocol_and_session():
    fake = _FakeLog()
    connection = protocol.RoomConnection("abc12", logger=fake)
    assert connection.logger is fake
    room_session = session.RoomSession("abc12", lambda kind, text: None, logger=fake)
    assert room_session.logger is fake
