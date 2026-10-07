"""What the FX module refuses, and how."""

from corridor.platform.errors import Conflict, InvalidRequest, NotFound, ServiceUnavailable


class RateUnavailable(ServiceUnavailable):
    """There is no rate fresh enough to quote from: the source could not be asked, or what
    it has is too old. Nothing was changed, and asking again later is safe."""

    code = "rate_unavailable"
    title = "Rate unavailable"

    def __init__(self) -> None:
        super().__init__("There is no current rate for this pair. Try again shortly.")


class AmountTooSmall(InvalidRequest):
    """The amount buys less than the smallest unit of the other asset. It would be taken
    and nothing given for it."""

    code = "amount_too_small"
    title = "Amount too small"

    def __init__(self) -> None:
        super().__init__("The amount is too small to convert.")


class SameAsset(InvalidRequest):
    code = "same_asset"
    title = "Same asset"

    def __init__(self) -> None:
        super().__init__("A conversion is between two different assets.")


class QuoteNotFound(NotFound):
    """There is no such quote, or there is and it is another user's. The two are never
    told apart."""

    code = "quote_not_found"
    title = "Quote not found"

    def __init__(self) -> None:
        super().__init__("There is no such quote.")


class QuoteAlreadyUsed(Conflict):
    code = "quote_already_used"
    title = "Quote already used"

    def __init__(self) -> None:
        super().__init__("This quote has already been converted.")


class QuoteExpired(Conflict):
    code = "quote_expired"
    title = "Quote expired"

    def __init__(self) -> None:
        super().__init__("This quote has expired. Ask for a new one.")


class ConversionNotFound(NotFound):
    """There is no such conversion, or there is and it is not this user's to see."""

    code = "conversion_not_found"
    title = "Conversion not found"

    def __init__(self) -> None:
        super().__init__("There is no such conversion.")


class DuplicateConversion(Exception):
    """A conversion id was used for a second conversion. A bug in the caller, which makes a
    new id for each attempt, and not something a client can cause."""
