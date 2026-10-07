"""Reading what a provider's webhook carried.

By the time a payload reaches this module its signature has been verified, so it did come
from the provider. That makes it authentic, not correct: it is still parsed against the
shape the provider contract gives it before any of it is used.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from corridor.payments.errors import MalformedProviderEvent
from corridor.platform.errors import InvalidRequest
from corridor.platform.money import parse_amount


class ProviderEvent(BaseModel):
    """The ``data`` of one webhook event. Nothing is coerced: an amount that arrives as a
    JSON number has been through a float and is refused. Fields the contract does not name
    are ignored, so a provider may add to its events."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)


def parse[E: ProviderEvent](model: type[E], data: Mapping[str, Any]) -> E:
    try:
        return model.model_validate(dict(data))
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in item["loc"]) for item in error.errors()})
        raise MalformedProviderEvent(
            f"{model.__name__}: invalid {', '.join(fields) or 'payload'}"
        ) from None


def amount_of(text: str, asset: str, *, allow_zero: bool = False) -> int:
    """A decimal string of the contract as minor units, exactly."""
    try:
        return parse_amount(text, asset, allow_zero=allow_zero)
    except InvalidRequest:
        # How platform.money refuses an amount or an asset code. Here it is the provider's
        # mistake and not a client's, and what was sent is not repeated.
        raise MalformedProviderEvent("invalid amount or asset") from None
