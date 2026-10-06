"""Injected faults: how a test makes a provider operation fail or hang.

Each provider endpoint asks this table twice, before it does its work and after. The two
``after_effect`` modes answer only the second time, so the caller sees a failure for an
operation that did happen: the case idempotency keys exist for.
"""

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Final, Literal

from corridor_sim.errors import ApiError

Mode = Literal["error", "timeout", "error_after_effect", "timeout_after_effect"]

OPERATIONS: Final = frozenset(
    {
        "bank.create_virtual_account",
        "bank.create_beneficiary",
        "bank.create_payout",
        "bank.get_payout",
        "bank.list_transactions",
        "custody.create_address",
        "custody.create_withdrawal",
        "custody.get_withdrawal",
        "custody.list_transactions",
        "fx.get_rate",
    }
)

_AFTER_EFFECT: Final = frozenset({"error_after_effect", "timeout_after_effect"})
_HANGS: Final = frozenset({"timeout", "timeout_after_effect"})


@dataclass(slots=True)
class Fault:
    operation: str
    mode: Mode
    # How many more calls this fault will spoil.
    times: int
    status: int
    hang_seconds: float


class FaultTable:
    """The faults waiting for each operation, oldest first."""

    def __init__(self) -> None:
        self._queued: dict[str, deque[Fault]] = {}

    def add(
        self, operation: str, mode: Mode, *, times: int, status: int, hang_seconds: float
    ) -> Fault:
        if operation not in OPERATIONS:
            raise ApiError(
                422, "unknown_operation", "That is not an operation a fault can be injected into."
            )
        fault = Fault(operation, mode, times, status, hang_seconds)
        self._queued.setdefault(operation, deque()).append(fault)
        return fault

    def clear(self) -> None:
        self._queued.clear()

    def queued(self) -> list[Fault]:
        return [fault for faults in self._queued.values() for fault in faults]

    async def before(self, operation: str) -> None:
        """Fail or hang instead of doing the work, if a fault says so."""
        await self._apply(operation, after_effect=False)

    async def after(self, operation: str) -> None:
        """Fail or hang instead of answering, once the work is done and recorded."""
        await self._apply(operation, after_effect=True)

    async def _apply(self, operation: str, *, after_effect: bool) -> None:
        fault = self._take(operation, after_effect=after_effect)
        if fault is None:
            return
        if fault.mode in _HANGS:
            # A real sleep: its purpose is to outlast the caller's real deadline. A caller
            # who gives up cancels it here, after everything else this call did.
            await asyncio.sleep(fault.hang_seconds)
            raise ApiError(504, "injected_timeout", "An injected fault made this call time out.")
        raise ApiError(fault.status, "injected_fault", "An injected fault made this call fail.")

    def _take(self, operation: str, *, after_effect: bool) -> Fault | None:
        """The fault this call meets at this point, counted against its ``times``."""
        faults = self._queued.get(operation)
        if not faults:
            return None
        fault = faults[0]
        if (fault.mode in _AFTER_EFFECT) != after_effect:
            return None
        fault.times -= 1
        if fault.times <= 0:
            faults.popleft()
        return fault
