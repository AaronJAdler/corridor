"""FX: mid rates, quotes and conversions between a user's own wallets.

Other modules use the names exported here and nothing else from this package.
"""

from corridor.fx.conversions import FX_CONVERTED, convert, get_conversion
from corridor.fx.errors import (
    AmountTooSmall,
    ConversionNotFound,
    DuplicateConversion,
    QuoteAlreadyUsed,
    QuoteExpired,
    QuoteNotFound,
    RateUnavailable,
    SameAsset,
)
from corridor.fx.quotes import check_pair, create_quote, purge_unused_quotes
from corridor.fx.rates import get_mid
from corridor.fx.rounding import buy_amount, customer_rate, format_rate
from corridor.fx.types import Conversion, Quote, QuoteStatus

__all__ = [
    "FX_CONVERTED",
    "AmountTooSmall",
    "Conversion",
    "ConversionNotFound",
    "DuplicateConversion",
    "Quote",
    "QuoteAlreadyUsed",
    "QuoteExpired",
    "QuoteNotFound",
    "QuoteStatus",
    "RateUnavailable",
    "SameAsset",
    "buy_amount",
    "check_pair",
    "convert",
    "create_quote",
    "customer_rate",
    "format_rate",
    "get_conversion",
    "get_mid",
    "purge_unused_quotes",
]
