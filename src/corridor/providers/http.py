"""The one HTTP client the adapters share, and the rules of the provider contract that
are the same for every provider.

This is a trust boundary. A response is parsed against a model and compared with what was
asked for before any of it is used, and whatever cannot be believed is reported as an
unknown outcome rather than as a failure: a provider that answers nonsense may still have
done what it was asked.

Nothing here logs a request or response body. Bodies carry account numbers.
"""

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal
from urllib.parse import quote

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, ValidationError

from corridor.platform.errors import InvalidRequest
from corridor.platform.logging import get_logger
from corridor.platform.metrics import PROVIDER_CALL_SECONDS, PROVIDER_CALLS
from corridor.platform.money import format_amount, parse_amount
from corridor.providers.errors import (
    ProviderMisconfigured,
    ProviderOutcomeUnknown,
    ProviderRejected,
)
from corridor.providers.types import ProviderStatement, ProviderTransaction

log = get_logger(__name__)

IDEMPOTENCY_HEADER: Final = "Idempotency-Key"

# The first answer to a POST is 201; a read, or a repeat that finds the resource, is 200.
_SUCCESS: Final = frozenset({200, 201})

# The most of a response body that is read. No answer in the contract is a hundredth of
# this; a body that goes on past it is not an answer, and reading it all would let whoever
# sent it choose how much memory this process uses.
MAX_RESPONSE_BYTES: Final = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _Answer:
    """What was read of a response: its status and its body, which is whole."""

    status_code: int
    content: bytes


class _UnreadableBody(Exception):
    """A response body that was not read to its end: it went past ``MAX_RESPONSE_BYTES``,
    or it was compressed, which nothing asked for."""


class Document(BaseModel):
    """The shape of a response body. Nothing is coerced: an amount that arrives as a JSON
    number has already been through a float, and is refused. Fields the contract does not
    name are ignored, so a provider may add to its responses."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)


class _ErrorDetail(Document):
    code: str = Field(min_length=1)
    message: str


class _ErrorDocument(Document):
    error: _ErrorDetail


class ResponseMismatch(ValueError):
    """A response is well-formed and is not an answer to the request that was sent."""


def require_echo(field: str, sent: object, received: object) -> None:
    """Refuse a response whose ``field`` is not what the request said.

    The message names the field and never the values: one of them may be an account number.
    """
    if received != sent:
        raise ResponseMismatch(f"'{field}' does not match the request")


class ProviderClient:
    """Bearer authentication, one deadline per call, and the contract's reading of status
    codes. One per adapter."""

    def __init__(
        self,
        *,
        provider: str,
        base_url: str | None,
        api_key: SecretStr | None,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._provider = provider
        if not base_url:
            raise self._misconfigured("configure", "no address is configured for this provider")
        if api_key is None or not api_key.get_secret_value():
            raise self._misconfigured("configure", "no API key is configured for this provider")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        # A client handed in belongs to whoever made it, and is not closed here.
        self._owns_client = client is None
        # A client of its own takes nothing from the environment: a proxy variable set
        # on the host would otherwise route every provider call, credentials and all,
        # through whatever it names.
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(timeout=timeout_seconds, trust_env=False, follow_redirects=False)
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get[D: Document, T](
        self,
        operation: str,
        path: str,
        model: type[D],
        convert: Callable[[D], T],
        *,
        params: Mapping[str, str] | None = None,
    ) -> T:
        return await self._call("GET", operation, path, model, convert, params=params)

    async def post[D: Document, T](
        self,
        operation: str,
        path: str,
        model: type[D],
        convert: Callable[[D], T],
        *,
        body: Mapping[str, object],
        idempotency_key: str,
    ) -> T:
        """Send a request that changes something. The key has no default on purpose: there
        is no mutating call that is safe to retry without one."""
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("a mutating provider call needs an idempotency key")
        return await self._call(
            "POST",
            operation,
            path,
            model,
            convert,
            body=body,
            headers={IDEMPOTENCY_HEADER: idempotency_key},
        )

    async def _call[D: Document, T](
        self,
        method: str,
        operation: str,
        path: str,
        model: type[D],
        convert: Callable[[D], T],
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> T:
        started = time.monotonic()
        status: int | None = None
        try:
            response = await self._send(method, operation, path, params, body, headers)
            status = response.status_code
            result = self._read(operation, response, model, convert)
        except ProviderRejected as refused:
            self._log(log.info, operation, status, started, "rejected", code=refused.code)
            raise
        except ProviderOutcomeUnknown as unknown:
            self._log(log.warning, operation, status, started, "unknown", reason=unknown.detail)
            raise
        except ProviderMisconfigured:
            self._log(log.error, operation, status, started, "misconfigured")
            raise
        self._log(log.info, operation, status, started, "ok")
        return result

    async def _send(
        self,
        method: str,
        operation: str,
        path: str,
        params: Mapping[str, str] | None,
        body: Mapping[str, object] | None,
        headers: Mapping[str, str] | None,
    ) -> _Answer:
        request = self._client.build_request(
            method,
            self._base_url + path,
            params=params,
            json=body,
            headers={
                **(headers or {}),
                "Authorization": f"Bearer {self._api_key.get_secret_value()}",
                # A compressed body has no size until it has been expanded.
                "Accept-Encoding": "identity",
            },
        )
        try:
            # The deadline covers the whole exchange. httpx's own timeouts bound each phase
            # separately, and an in-process transport does not enforce them at all.
            async with asyncio.timeout(self._timeout_seconds):
                # A redirect is never followed, whatever the client was built to do: the
                # request carries the API key, and would carry it wherever it was sent.
                response = await self._client.send(request, stream=True, follow_redirects=False)
                try:
                    return _Answer(response.status_code, await _read_capped(response))
                finally:
                    await response.aclose()
        except TimeoutError:
            raise self._unknown(operation, "no response within the deadline") from None
        except _UnreadableBody as unreadable:
            raise self._unknown(operation, f"invalid response: {unreadable}") from None
        except (httpx.HTTPError, OSError) as error:
            # Only the kind of failure: the text of a transport error can quote the URL.
            raise self._unknown(operation, f"transport error: {type(error).__name__}") from None

    def _read[D: Document, T](
        self, operation: str, response: _Answer, model: type[D], convert: Callable[[D], T]
    ) -> T:
        status = response.status_code
        if status == 401:
            # The provider answers this to every call alike, whatever was asked: it is a
            # fact about Corridor's configuration, and the caller must not take it as the
            # provider's judgement of this payout or this address.
            raise self._misconfigured(operation, "the provider did not accept the API key")
        if 400 <= status < 500:
            raise self._refusal(operation, response)
        if status not in _SUCCESS:
            # A 5xx says nothing about whether the operation happened.
            raise self._unknown(operation, f"the provider answered {status}")
        try:
            return convert(model.model_validate_json(response.content))
        except ValidationError as error:
            raise self._unknown(operation, f"invalid response: {_fields(error)}") from None
        except (ResponseMismatch, InvalidRequest) as error:
            # InvalidRequest is how platform.money refuses an amount or an asset code.
            raise self._unknown(operation, f"unbelievable response: {error}") from None

    def _refusal(self, operation: str, response: _Answer) -> Exception:
        """A ``4xx`` is a definite refusal only if it is the provider's own. One without the
        contract's error body came from something in between, and proves nothing."""
        try:
            error = _ErrorDocument.model_validate_json(response.content).error
        except ValidationError:
            return self._unknown(
                operation, f"a {response.status_code} without the contract's error body"
            )
        return ProviderRejected(
            error.code,
            error.message,
            response.status_code,
            provider=self._provider,
            operation=operation,
        )

    def _unknown(self, operation: str, detail: str) -> ProviderOutcomeUnknown:
        return ProviderOutcomeUnknown(detail, provider=self._provider, operation=operation)

    def _misconfigured(self, operation: str, detail: str) -> ProviderMisconfigured:
        return ProviderMisconfigured(detail, provider=self._provider, operation=operation)

    def _log(
        self,
        emit: Callable[..., object],
        operation: str,
        status: int | None,
        started: float,
        outcome: str,
        **extra: str,
    ) -> None:
        elapsed = time.monotonic() - started
        emit(
            "provider.call",
            provider=self._provider,
            operation=operation,
            status=status,
            duration_ms=round(elapsed * 1000),
            outcome=outcome,
            **extra,
        )
        # The operation is a name from the adapter's code, never anything a provider or a
        # client sent, so the labels are a fixed set.
        PROVIDER_CALLS.labels(provider=self._provider, operation=operation, outcome=outcome).inc()
        PROVIDER_CALL_SECONDS.labels(provider=self._provider, operation=operation).observe(elapsed)


async def _read_capped(response: httpx.Response) -> bytes:
    """The body of a response that is being streamed, or ``_UnreadableBody``.

    What the response says about its own length is not asked: the count is of the bytes
    that arrive. They are taken as they arrive and never expanded, so a small compressed
    body cannot become a large one here.
    """
    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
        raise _UnreadableBody("body is compressed")
    if response.is_stream_consumed:
        # Handed over whole by a transport that does not stream, as an in-process one may.
        # There is nothing left to refuse to read, only to refuse to use.
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise _UnreadableBody("body too large")
        return response.content
    body = bytearray()
    async for chunk in response.aiter_raw():
        body += chunk
        if len(body) > MAX_RESPONSE_BYTES:
            raise _UnreadableBody("body too large")
    return bytes(body)


def _fields(error: ValidationError) -> str:
    """Which fields were wrong, and never what they held."""
    names = {".".join(str(part) for part in item["loc"]) or "body" for item in error.errors()}
    return ", ".join(sorted(names))


# --- the contract's conventions --------------------------------------------------------------


def major_units(amount: int, asset_code: str) -> str:
    """An amount as the contract writes it in a request: a decimal string in major units."""
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValueError("an amount sent to a provider is a positive int of minor units")
    return format_amount(amount, asset_code)


def minor_units(text: str, asset_code: str, *, allow_zero: bool = False) -> int:
    """An amount from a response, exactly. A fee may be zero; a movement may not."""
    return parse_amount(text, asset_code, allow_zero=allow_zero)


def signed_minor_units(text: str, asset_code: str) -> int:
    """A balance from a response. It may be negative: Corridor can be overdrawn."""
    magnitude = parse_amount(text.removeprefix("-"), asset_code, allow_zero=True)
    return -magnitude if text.startswith("-") else magnitude


def timestamp(moment: datetime) -> str:
    """A time as the contract writes it: ISO-8601 in UTC with a ``Z``."""
    if moment.tzinfo is None:
        raise ValueError("a time sent to a provider needs a timezone")
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat() + "Z"


def in_utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)


def optional_utc(moment: datetime | None) -> datetime | None:
    return moment.astimezone(UTC) if moment is not None else None


def segment(identifier: str) -> str:
    """An identifier as one path segment, so that no id can name another resource."""
    if not identifier:
        raise ValueError("an identifier is not empty")
    if identifier in (".", ".."):
        # Quoting leaves a dot as it is, and a client or a proxy that normalises the path
        # would read these two as "here" and "the resource above".
        raise ValueError("an identifier is not a relative path")
    return quote(identifier, safe="")


def searchable(reference: str) -> str:
    if not reference:
        raise ValueError("a reference to search by is not empty")
    return reference


# --- the statement, which both the bank and the custodian serve in one shape ----------------


class TransactionDocument(Document):
    id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    direction: Literal["credit", "debit"]
    asset: str
    amount: str
    reference: str | None
    related_id: str | None
    occurred_at: AwareDatetime
    tx_hash: str | None = None


class StatementDocument(Document):
    asset: str
    start: AwareDatetime = Field(alias="from")
    end: AwareDatetime = Field(alias="to")
    transactions: list[TransactionDocument]
    closing_balance: str


def statement_params(asset_code: str, start: datetime, end: datetime) -> dict[str, str]:
    return {"asset": asset_code, "from": timestamp(start), "to": timestamp(end)}


def statement(
    document: StatementDocument, *, asset_code: str, start: datetime, end: datetime
) -> ProviderStatement:
    require_echo("asset", asset_code, document.asset)
    require_echo("from", start, document.start)
    require_echo("to", end, document.end)
    for line in document.transactions:
        require_echo("transactions.asset", asset_code, line.asset)
    return ProviderStatement(
        asset_code=document.asset,
        start=in_utc(document.start),
        end=in_utc(document.end),
        transactions=tuple(
            ProviderTransaction(
                id=line.id,
                type=line.type,
                direction=line.direction,
                asset_code=line.asset,
                amount=minor_units(line.amount, line.asset),
                reference=line.reference,
                related_id=line.related_id,
                occurred_at=in_utc(line.occurred_at),
                tx_hash=line.tx_hash,
            )
            for line in document.transactions
        ),
        closing_balance=signed_minor_units(document.closing_balance, document.asset),
    )
