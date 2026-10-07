"""Reading what a provider's webhook carried.

By the time a payload reaches this module its signature has been verified, so it did come
from the provider. That makes it authentic, not correct: it is still parsed against the
shape the provider contract gives it before any of it is used.
"""

import re
from collections.abc import Mapping
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

from corridor.payments.errors import MalformedProviderEvent
from corridor.platform.errors import InvalidRequest
from corridor.platform.money import parse_amount

# What a reason from a provider has to look like to be kept: a short code, as the contract
# gives, and not a sentence.
_REASON_CODE: Final = re.compile(r"[a-z0-9_.:-]{1,64}", re.ASCII)
UNSPECIFIED_REASON: Final = "unspecified"


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


def reason_code(text: object) -> str:
    """A provider's reason for a failure or a return, if it is a code, and ``unspecified``
    if it is anything else.

    The field is free text chosen by someone outside Corridor, and it is stored, audited
    and shown to the user. Whatever is not a plain code is dropped and not cleaned up.
    """
    if isinstance(text, str) and _REASON_CODE.fullmatch(text) is not None:
        return text
    return UNSPECIFIED_REASON
