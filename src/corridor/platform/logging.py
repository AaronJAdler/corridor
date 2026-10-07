"""Structured logging.

One JSON object per line on stdout, with the request id and principal bound from context.
Every event passes through ``redact`` before it is rendered, so a secret that reaches a log
call by mistake does not reach the log.
"""

import dataclasses
import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any, Final

import structlog
from pydantic import BaseModel
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

# Shorter names, which are sensitive only as a whole word of the key: "pass" in "user_pass"
# but not in "passed", "sig" in "webhook_sig" but not in "signal".
_SENSITIVE_WORDS: Final = frozenset(
    {"pwd", "pass", "jwt", "pem", "hash", "digest", "sig", "account", "acct", "routing", "pix"}
)
# A key that ends in one of these names a row, not what the row holds: "account_id".
_REFERENCE_WORDS: Final = frozenset({"id", "ids"})
# Where one word of a key ends and the next begins: a separator, or a capital in camelCase.
_WORD_BREAK: Final = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")

# Values that are secrets whatever key they sit under.
_SENSITIVE_VALUE: Final = re.compile(
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"  # a JWT
    r"|\bck_[A-Za-z0-9-]{3,}_[A-Za-z0-9_-]{16,}"  # an agent API key
    r"|\b(?i:bearer)\s+[A-Za-z0-9._~+/=-]{8,}"  # a bearer credential
    # Basic credentials are base64, which is never all lower-case letters. Asking for
    # that keeps "a basic understanding" in one piece.
    r"|\b(?i:basic)\s+(?=[a-z]*[A-Z0-9+/=])[A-Za-z0-9+/=]{8,}"
    # user:password@ in a URL. The user may be empty and the password may hold a slash.
    r"|\b[a-z][a-z0-9+.-]*://[^\s/:@]*:[^\s@]+@"
    r"|\$argon2(?:id|i|d)\$[^\s\"']+"  # an Argon2 hash
    r"|\bv[0-9]+=[0-9A-Fa-f]{64}\b"  # a versioned webhook signature
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
)

# A name and its value written out inside a string: `password=x` in a query string or a
# repr, `"password": "x"` in JSON, with the quotes escaped or not. The name is judged as a
# key would be, and only the value is removed.
_ASSIGNMENT: Final = re.compile(
    r"""(?<![A-Za-z0-9_-])
        (?P<name>[A-Za-z][A-Za-z0-9_-]*)
        (?P<between>\\?["']?\s*[=:]\s*)
        (?!\[redacted\])
        (?P<value>
            \\"[^"]*\\"
          | "(?:[^"\\]|\\.)*"
          | '(?:[^'\\]|\\.)*'
          | [^\s&,;)}\]"'\\]+
          | ["']\S*
        )""",
    re.VERBOSE,
)

# A word as it appears in a format string, for judging what its arguments may be.
_WORD: Final = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


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
        return _scrub_text(value)
    if isinstance(value, Mapping):
        return {key: _scrub_under(key, item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_scrub_item(item) for item in value]
    if isinstance(value, bytes | bytearray):
        return REDACTED
    if isinstance(value, BaseModel):
        # Read field by field and not through its repr, so each value is judged by the
        # name it is held under.
        return _scrub({name: getattr(value, name) for name in type(value).model_fields})
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _scrub(
            {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
        )
    if isinstance(value, BaseException):
        return _scrub_text(repr(value))
    return value


def _scrub_text(text: str) -> str:
    text = _SENSITIVE_VALUE.sub(REDACTED, text)
    kept: list[str] = []
    position = 0
    while (match := _ASSIGNMENT.search(text, position)) is not None:
        if _is_sensitive_key(match["name"]):
            kept.append(text[position : match.start("value")] + REDACTED)
            position = match.end()
        else:
            # Only the name is passed over. What follows it may itself be a name and a
            # value: "refused: password=x".
            kept.append(text[position : match.end("name")])
            position = match.end("name")
    kept.append(text[position:])
    return "".join(kept)


def _scrub_under(key: object, value: Any) -> Any:
    """The value as it may be shown under ``key``."""
    # A yes or a no holds no secret, and "signature_valid" is worth reading.
    if _is_sensitive_key(key) and not isinstance(value, bool):
        return REDACTED
    return _scrub(value)


def _scrub_item(item: Any) -> Any:
    # A name and its value as a pair, the way headers and form fields are often held.
    is_pair = isinstance(item, list | tuple) and len(item) == 2 and isinstance(item[0], str)
    if is_pair:
        return [item[0], _scrub_under(item[0], item[1])]
    return _scrub(item)


def _is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    if _SENSITIVE_KEY.search(key) is not None:
        return True
    words = [word.lower() for word in _WORD_BREAK.split(key) if word]
    return (
        bool(words) and words[-1] not in _REFERENCE_WORDS and not _SENSITIVE_WORDS.isdisjoint(words)
    )


def scrub(value: Any) -> Any:
    """A copy of ``value`` with secrets removed. For data that is stored rather than logged,
    such as audit details and recorded error messages."""
    return _scrub(value)


def redact(_logger: WrappedLogger, _method: str, event: EventDict) -> EventDict:
    """Remove secrets from a log event, by key name and by the shape of the value."""
    if event.get("_from_structlog") is False:
        _redact_arguments(event)
    for key in list(event):
        event[key] = _scrub_under(key, event[key])
    return event


def _redact_arguments(event: EventDict) -> None:
    """Deal with the arguments of a record from the standard library's logging.

    Its message arrives already formatted, and "the password is %s" leaves nothing in the
    result to tell the password by. So the format string is read instead: if it names
    anything sensitive, the message is formatted again with every argument removed.
    """
    arguments = event.pop("positional_args", None)
    record = event.get("_record")
    if not arguments or not isinstance(record, logging.LogRecord):
        return
    template = str(record.msg)
    if not any(_is_sensitive_key(word) for word in _WORD.findall(template)):
        return
    removed: Any = (
        dict.fromkeys(arguments, REDACTED)
        if isinstance(arguments, Mapping)
        else tuple(REDACTED for _ in arguments)
    )
    try:
        event["event"] = template % removed
    except TypeError, ValueError:
        # A placeholder that wants a number. The words of the message are still worth having.
        event["event"] = f"{template} ({REDACTED})"


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
            # So that `redact` can see what a foreign record was formatted with.
            pass_foreign_args=True,
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
    # httpx logs every request URL at INFO; provider URLs carry ids and references.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind_context(**values: object) -> None:
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


def current_context() -> MutableMapping[str, Any]:
    return structlog.contextvars.get_contextvars()
