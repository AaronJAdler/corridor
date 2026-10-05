"""Structured logging.

One JSON object per line on stdout, with the request id and principal bound from context.
Every event passes through ``redact`` before it is rendered, so a secret that reaches a log
call by mistake does not reach the log.
"""

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any, Final

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

REDACTED: Final = "[redacted]"

# A key whose name contains one of these never has its value logged.
_SENSITIVE_KEY: Final = re.compile(
    r"password|passwd|passphrase|secret|token|authorization|cookie"
    r"|api[_-]?key|private[_-]?key|signing[_-]?key|signature|pepper|credential"
    r"|dsn|database[_-]?url|redis[_-]?url"
    r"|account[_-]?number|routing[_-]?number|iban|clabe|pix[_-]?key|card[_-]?number|ssn",
    re.IGNORECASE,
)

# Values that are secrets whatever key they sit under.
_SENSITIVE_VALUE: Final = re.compile(
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"  # a JWT
    r"|\bck_[A-Za-z0-9]{4,}_[A-Za-z0-9_-]{16,}"  # an agent API key
    r"|\b[Bb]earer\s+[A-Za-z0-9._~+/=-]{8,}"  # a bearer credential
    r"|\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"  # user:password@ in a URL
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
)


class _StdoutHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to whatever ``sys.stdout`` is at the time, not what it was at start-up."""

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self) -> Any:
        return sys.stdout

    @stream.setter
    def stream(self, _value: Any) -> None:
        pass


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return _SENSITIVE_VALUE.sub(REDACTED, value)
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _is_sensitive_key(key) else _scrub(item) for key, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [_scrub(item) for item in value]
    if isinstance(value, bytes | bytearray):
        return REDACTED
    return value


def _is_sensitive_key(key: object) -> bool:
    return isinstance(key, str) and _SENSITIVE_KEY.search(key) is not None


def redact(_logger: WrappedLogger, _method: str, event: EventDict) -> EventDict:
    """Remove secrets from a log event, by key name and by the shape of the value."""
    for key in list(event):
        event[key] = REDACTED if _is_sensitive_key(key) else _scrub(event[key])
    return event


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Route structlog and the standard library's loggers through one redacting pipeline."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact,
    ]
    renderer: Processor = (
        structlog.dev.ConsoleRenderer(colors=False)
        if fmt == "console"
        else structlog.processors.JSONRenderer(sort_keys=True)
    )

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = _StdoutHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )
    root = logging.getLogger()
    # Replace only a handler installed by an earlier call, so configuring twice is harmless
    # and handlers that belong to someone else (a test runner's, say) are left alone.
    root.handlers = [h for h in root.handlers if not isinstance(h, _StdoutHandler)] + [handler]
    root.setLevel(level)

    # Uvicorn installs its own handlers; hand its records to the root pipeline instead. The
    # access log is replaced by the request middleware, which logs the route template.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind_context(**values: object) -> None:
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


def current_context() -> MutableMapping[str, Any]:
    return structlog.contextvars.get_contextvars()
