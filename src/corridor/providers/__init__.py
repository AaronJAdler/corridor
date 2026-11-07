"""Providers: the ports Corridor needs from a bank rail, a custodian and a rate source, and
the adapters that implement them.

Other modules use the names exported here and nothing else from this package, and get an
adapter only from the ``build_*`` functions, which choose one by the ``*_provider`` settings.
The adapters are exported for the test suite alone: a module of ``corridor``
outside this package that names one is tied to a single provider, and
``tests/providers/test_ports_only.py`` refuses it. A call made through an adapter is a
network call: never make one while a transaction is open.
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
from corridor.providers.factory import build_bank_rail, build_custodian, build_rate_source
from corridor.providers.fx_rates import SimRates
from corridor.providers.increase import EVENT_CATEGORIES as INCREASE_EVENT_CATEGORIES
from corridor.providers.increase import IncreaseBank
from corridor.providers.names import (
    BANK_RAILS,
    CUSTODIANS,
    INCREASE,
    SIM_BANK,
    SIM_CUSTODY,
    SIM_RATES,
    FlowKind,
    flow_kind,
    is_bank_rail,
    require_bank_rail,
)
from corridor.providers.ports import BankEventSource, BankRail, Custodian, RateSource
from corridor.providers.types import (
    BankEvent,
    BankEventType,
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
    "BANK_RAILS",
    "CUSTODIANS",
    "INCREASE",
    "INCREASE_EVENT_CATEGORIES",
    "SIM_BANK",
    "SIM_CUSTODY",
    "SIM_RATES",
    "BankEvent",
    "BankEventSource",
    "BankEventType",
    "BankRail",
    "Beneficiary",
    "Custodian",
    "DepositAddress",
    "FlowKind",
    "IncreaseBank",
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
    "build_bank_rail",
    "build_custodian",
    "build_rate_source",
    "flow_kind",
    "is_bank_rail",
    "is_valid_address",
    "require_bank_rail",
]
