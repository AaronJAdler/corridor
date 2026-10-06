"""Providers: the ports Corridor needs from a bank rail, a custodian and a rate source, and
the adapters that implement them over HTTP for the simulated providers.

Other modules use the names exported here and nothing else from this package. A call made
through an adapter is a network call: never make one while a transaction is open.
"""

from corridor.providers.addresses import is_valid_address
from corridor.providers.bank_rail import SimBank
from corridor.providers.custody import SimCustody
from corridor.providers.errors import (
    ProviderError,
    ProviderMisconfigured,
    ProviderOutcomeUnknown,
    ProviderRejected,
)
from corridor.providers.fx_rates import SimRates
from corridor.providers.ports import BankRail, Custodian, RateSource
from corridor.providers.types import (
    Beneficiary,
    DepositAddress,
    Payout,
    PayoutStatus,
    ProviderStatement,
    ProviderTransaction,
    Rate,
    TransactionDirection,
    VirtualAccount,
    Withdrawal,
    WithdrawalStatus,
)

__all__ = [
    "BankRail",
    "Beneficiary",
    "Custodian",
    "DepositAddress",
    "Payout",
    "PayoutStatus",
    "ProviderError",
    "ProviderMisconfigured",
    "ProviderOutcomeUnknown",
    "ProviderRejected",
    "ProviderStatement",
    "ProviderTransaction",
    "Rate",
    "RateSource",
    "SimBank",
    "SimCustody",
    "SimRates",
    "TransactionDirection",
    "VirtualAccount",
    "Withdrawal",
    "WithdrawalStatus",
    "is_valid_address",
]
