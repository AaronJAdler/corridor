"""The one place that chooses which adapter stands behind each port.

The API and the worker ask for a port here and hold it as a port. Nothing else outside this
package names an adapter, and ``poe contracts`` refuses an import that does. Which adapter
is chosen comes from the ``*_provider`` settings, and each of those defaults to the
simulator.
"""

from corridor.platform.config import INCREASE_SANDBOX_URL, Settings
from corridor.providers.bank_rail import SimBank
from corridor.providers.custody import SimCustody
from corridor.providers.errors import ProviderMisconfigured
from corridor.providers.fx_rates import SimRates
from corridor.providers.increase import IncreaseBank
from corridor.providers.names import INCREASE
from corridor.providers.ports import BankRail, Custodian, RateSource


def build_bank_rail(settings: Settings) -> BankRail | None:
    """The bank rail, or None when it is not configured.

    The simulator is not configured without an address, and is left out. A sandbox adapter
    is configured by its mode alone: choosing it is saying that it is wanted.
    """
    if settings.bank_rail_provider == "increase_sandbox":
        _require_increase_sandbox(settings)
        # Never the simulator in its place: a process that asked for the sandbox and cannot
        # have it does not start.
        return IncreaseBank(settings)
    return SimBank(settings) if settings.bank_rail_url else None


def build_custodian(settings: Settings) -> Custodian | None:
    """The custodian, or None when it is not configured."""
    return SimCustody(settings) if settings.custody_url else None


def build_rate_source(settings: Settings) -> RateSource | None:
    """The rate source, or None when it is not configured."""
    return SimRates(settings) if settings.fx_rates_url else None


def _require_increase_sandbox(settings: Settings) -> None:
    """Refuse any address but the sandbox's, a second time.

    The settings refuse it when they are loaded. This is the check made at the point of
    use, so that a settings object built some other way cannot point the adapter at another
    host. The address is compared exactly, and never taken from the key.
    """
    if settings.increase_base_url != INCREASE_SANDBOX_URL:
        raise ProviderMisconfigured(
            "increase_base_url is not the Increase sandbox address",
            provider=INCREASE,
            operation="build",
        )
