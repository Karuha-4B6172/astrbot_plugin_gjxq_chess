"""Logger plumbing for the ``chess_site`` package.

AstrBot plugin guidelines require logging to go through ``astrbot.api.logger``;
``main.py`` therefore injects that logger into :class:`chess_site.session.RoomSession`,
which forwards it to :class:`chess_site.protocol.RoomConnection`.

The ``chess_site`` package is also used standalone by the offline test-suite
(no AstrBot runtime available), so a silent sink is used when no logger is
injected.  This module deliberately does **not** import the standard ``logging``
module: in the plugin every log record must come from ``astrbot.api.logger``.
"""

from __future__ import annotations


class SilentLog:
    """No-op log sink used by standalone/test usage when no logger is injected.

    It intentionally offers the small subset of the logging interface that the
    package uses.  In the AstrBot plugin the real ``astrbot.api.logger`` object is
    passed in instead, so this sink is never used there.
    """

    __slots__ = ()

    def debug(self, *args, **kwargs) -> None:  # noqa: D401 - logging signature
        """Ignore a debug message."""

    def info(self, *args, **kwargs) -> None:
        """Ignore an info message."""

    def warning(self, *args, **kwargs) -> None:
        """Ignore a warning message."""

    def error(self, *args, **kwargs) -> None:
        """Ignore an error message."""

    def exception(self, *args, **kwargs) -> None:
        """Ignore an exception message."""

    def critical(self, *args, **kwargs) -> None:
        """Ignore a critical message."""
