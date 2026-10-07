"""A reconciliation run: what each provider says happened, against what Corridor recorded.

A run is an entry point and works in three steps. It reads the providers with no
transaction open: each one's statement, and what it holds for every withdrawal still in
flight. It then compares and records, in one transaction. Then it repairs what it can.

Records are matched by the provider's own id for them and never by time, because the two
sides do not book a movement at the same moment: a deposit is on the bank's statement
before its webhook has been processed. For the same reason a statement is read from some
way before the window: a record Corridor made in the window may be of something the
provider did before it.

Time does enter in one place. A deposit the provider has only just received is not called
missing yet: the webhook that reports it is given a while to arrive first.
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from corridor import ledger, payments
from corridor.ledger import Account, AccountKind
from corridor.payments import Deposit, FlowKind, Withdrawal
from corridor.platform.clock import utcnow
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.platform.logging import get_logger
from corridor.platform.metrics import RECON_BREAK_CHANGES, RECON_BREAKS
from corridor.platform.money import ASSETS
from corridor.providers import (
    BankRail,
    Custodian,
    Payout,
    ProviderError,
    ProviderRejected,
    ProviderStatement,
    ProviderTransaction,
)
from corridor.providers import Withdrawal as CustodyWithdrawal
from corridor.recon import breaks, repair
from corridor.recon.types import Break, BreakKind, Finding, Run, RunResult, Sent

log = get_logger(__name__)

# How far before the window a provider's statement is read. A record made here in the
# window is matched against it, so anything Corridor books later than this after the
# provider did is reported as unknown to the provider, for a person to look at.
LOOKBACK: Final = timedelta(hours=24)

# The longest window a scheduled run reaches back over to cover what earlier runs missed.
# Further back than this is for a person to reconcile, with a window they choose.
MAX_CATCH_UP: Final = timedelta(days=7)

# How many of Corridor's own records of one provider and asset a run reads for a window.
SCAN_LIMIT: Final = 10_000

_PAGE: Final = 200
_NOT_FOUND: Final = 404

_DEPOSIT: Final = "deposit"
_DEPOSIT_RETURN: Final = "deposit_return"
# What each kind of provider calls money it sent out, and what it charged for sending it.
_PAID: Final[Mapping[FlowKind, str]] = {"bank": "payout", "chain": "withdrawal"}
_FEES: Final = frozenset({"payout_fee", "network_fee"})

_PROVIDER: Final[Mapping[FlowKind, str]] = {
    "bank": payments.BANK_PROVIDER,
    "chain": payments.CUSTODY_PROVIDER,
}
# Where the ledger keeps what is at each kind of provider, and what such a provider moves.
_SETTLEMENT: Final[Mapping[FlowKind, AccountKind]] = {
    "bank": AccountKind.BANK_SETTLEMENT,
    "chain": AccountKind.CUSTODY_OMNIBUS,
}
_ASSET_KIND: Final[Mapping[FlowKind, str]] = {"bank": "fiat", "chain": "stablecoin"}

_IN_FLIGHT: Final = frozenset({"submitting", "submitted"})

_Statement = Callable[[str, datetime, datetime], Awaitable[ProviderStatement]]
_Lookup = Callable[[Withdrawal], Awaitable[Sent | None]]


@dataclass(frozen=True, slots=True)
class _View:
    """What one provider said, read before the comparison began."""

    kind: FlowKind
    provider: str
    statements: Mapping[str, ProviderStatement]
    # What the provider holds for the withdrawals in flight, by withdrawal.
    sent: Mapping[uuid.UUID, Sent]
    # Withdrawals recorded as sent whose payout the provider says it does not have.
    gone: frozenset[uuid.UUID]


async def run(
    db: Database,
    bank: BankRail | None,
    custody: Custodian | None,
    *,
    window_start: datetime,
    window_end: datetime,
    grace: timedelta = timedelta(0),
) -> RunResult:
    """Reconcile the half-open window ``[window_start, window_end)`` and repair what can be.

    A deposit the provider received less than ``grace`` ago and Corridor has not recorded
    is left alone for now: its webhook is most likely on its way, and says who sent it,
    which a statement does not. A later run finds it if the webhook never comes.

    A provider this process was not given is left out. One that cannot be read is left out
    for what could not be read, and the run is recorded as incomplete. A disagreement that
    already has an open break does not get a second one, so a run can be repeated, and two
    can overlap.

    The balance of each settlement account is compared as it was at ``window_end``: what
    the ledger posted since is taken off, and what the provider had booked by then and the
    ledger had not is allowed for.
    """
    if window_start >= window_end:
        raise ValueError("a reconciliation window ends after it starts")
    started_at = utcnow()

    in_flight = await db.run(
        lambda session: payments.withdrawals_in_flight(session, limit=repair.MAX_IN_FLIGHT)
    )
    complete = len(in_flight) < repair.MAX_IN_FLIGHT
    views: list[_View] = []
    # No transaction is open while the providers are read.
    if bank is not None:
        view, read = await _read(
            "bank", _bank_statement(bank), _bank_lookup(bank), in_flight, window_start, window_end
        )
        views.append(view)
        complete = complete and read
    if custody is not None:
        view, read = await _read(
            "chain",
            _custody_statement(custody),
            _custody_lookup(custody),
            in_flight,
            window_start,
            window_end,
        )
        views.append(view)
        complete = complete and read

    async def compare(
        session: AsyncSession,
    ) -> tuple[Run, list[tuple[Break, Finding]], list[BreakKind], list[BreakKind]]:
        findings: dict[tuple[str, str, str], Finding] = {}
        for seen in views:
            for asset, statement in seen.statements.items():
                for finding in await _compare(
                    session, seen, asset, statement, window_start, window_end, started_at - grace
                ):
                    findings.setdefault(finding.key, finding)

        run_id = new_id()
        found: list[tuple[Break, Finding]] = []
        opened: list[BreakKind] = []
        changed: list[BreakKind] = []
        # In one order, so that two runs opening the same breaks queue and do not deadlock.
        for key in sorted(findings):
            recorded, is_new, has_changed = await breaks.open_break(session, run_id, findings[key])
            found.append((recorded, findings[key]))
            if is_new:
                opened.append(recorded.kind)
            if has_changed:
                changed.append(recorded.kind)
        recorded_run = await breaks.record_run(
            session,
            run_id=run_id,
            window_start=window_start,
            window_end=window_end,
            status="completed" if complete else "incomplete",
            breaks_found=len(found),
            breaks_opened=len(opened),
            started_at=started_at,
        )
        return recorded_run, found, opened, changed

    recorded_run, found, opened_kinds, changed_kinds = await db.run(compare)
    # After the commit, and so once: the transaction above may have been run again.
    for kind in opened_kinds:
        RECON_BREAKS.labels(kind=kind).inc()
    for kind in changed_kinds:
        RECON_BREAK_CHANGES.labels(kind=kind).inc()
    repaired = await repair.repair(db, found, complete=complete)
    log.info(
        "recon.run_finished",
        run_id=str(recorded_run.id),
        status=recorded_run.status,
        breaks_found=recorded_run.breaks_found,
        breaks_opened=recorded_run.breaks_opened,
        repaired=repaired,
    )
    return RunResult(run=recorded_run, breaks=tuple(item for item, _ in found), repaired=repaired)


async def catch_up_start(
    session: AsyncSession, *, window_end: datetime, window: timedelta
) -> datetime:
    """Where a scheduled run's window starts: ``window`` before its end, or where the last
    completed run's window ended if that is further back, so that an outage longer than
    the window leaves nothing uncompared. Never more than ``MAX_CATCH_UP`` back."""
    start = window_end - window
    covered_until = await breaks.last_completed_window_end(session)
    if covered_until is not None and covered_until < start:
        start = covered_until
    return max(start, window_end - MAX_CATCH_UP)


# --- reading the providers -------------------------------------------------------------------


async def _read(
    kind: FlowKind,
    statement_of: _Statement,
    lookup: _Lookup,
    in_flight: list[Withdrawal],
    window_start: datetime,
    window_end: datetime,
) -> tuple[_View, bool]:
    """One provider's statements and what it holds for the withdrawals in flight, and
    whether all of it could be read."""
    provider = _PROVIDER[kind]
    complete = True

    statements: dict[str, ProviderStatement] = {}
    for asset in ASSETS.values():
        if asset.kind != _ASSET_KIND[kind]:
            continue
        try:
            statements[asset.code] = await statement_of(
                asset.code, window_start - LOOKBACK, window_end
            )
        except ProviderError as error:
            complete = False
            log.warning(
                "recon.provider_failed",
                provider=error.provider,
                operation=error.operation,
                asset=asset.code,
            )

    sent: dict[uuid.UUID, Sent] = {}
    gone: set[uuid.UUID] = set()
    for withdrawal in in_flight:
        if withdrawal.kind != kind:
            continue
        try:
            held = await lookup(withdrawal)
        except ProviderRejected as refusal:
            if refusal.status == _NOT_FOUND and withdrawal.provider_ref is not None:
                gone.add(withdrawal.id)
            else:
                complete = False
            continue
        except ProviderError as error:
            complete = False
            log.warning(
                "recon.provider_failed",
                provider=error.provider,
                operation=error.operation,
                withdrawal_id=str(withdrawal.id),
            )
            continue
        if held is None:
            continue
        if held.reference != str(withdrawal.id):
            # The payout recorded on the withdrawal is somebody else's. Nothing is done on
            # the strength of it.
            log.error("recon.payout_mismatch", withdrawal_id=str(withdrawal.id))
            complete = False
            continue
        sent[withdrawal.id] = held
    return _View(kind, provider, statements, sent, frozenset(gone)), complete


def _bank_statement(bank: BankRail) -> _Statement:
    async def statement_of(asset: str, start: datetime, end: datetime) -> ProviderStatement:
        return await bank.list_transactions(asset_code=asset, start=start, end=end)

    return statement_of


def _custody_statement(custody: Custodian) -> _Statement:
    async def statement_of(asset: str, start: datetime, end: datetime) -> ProviderStatement:
        return await custody.list_transactions(asset_code=asset, start=start, end=end)

    return statement_of


def _bank_lookup(bank: BankRail) -> _Lookup:
    async def lookup(withdrawal: Withdrawal) -> Sent | None:
        if withdrawal.provider_ref is not None:
            return _of_payout(await bank.get_payout(withdrawal.provider_ref))
        # Never recorded as sent. One key makes one payout; more is nothing to choose among.
        found = await bank.find_payouts(str(withdrawal.id))
        return _of_payout(found[0]) if len(found) == 1 else None

    return lookup


def _custody_lookup(custody: Custodian) -> _Lookup:
    async def lookup(withdrawal: Withdrawal) -> Sent | None:
        if withdrawal.provider_ref is not None:
            return _of_withdrawal(await custody.get_withdrawal(withdrawal.provider_ref))
        found = await custody.find_withdrawals(str(withdrawal.id))
        return _of_withdrawal(found[0]) if len(found) == 1 else None

    return lookup


def _of_payout(payout: Payout) -> Sent:
    return Sent(
        id=payout.id,
        status=payout.status,
        asset=payout.asset_code,
        amount=payout.amount,
        fee=payout.fee,
        reference=payout.reference,
        settled_at=payout.settled_at,
        failure_reason=payout.failure_reason,
        tx_hash=None,
    )


def _of_withdrawal(withdrawal: CustodyWithdrawal) -> Sent:
    return Sent(
        id=withdrawal.id,
        # Broadcast is still on its way: neither final nor failed.
        status="pending" if withdrawal.status == "broadcast" else withdrawal.status,
        asset=withdrawal.asset_code,
        amount=withdrawal.amount,
        fee=withdrawal.network_fee,
        reference=withdrawal.reference,
        settled_at=withdrawal.completed_at,
        failure_reason=withdrawal.failure_reason,
        tx_hash=withdrawal.tx_hash,
    )


# --- comparing -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Sides:
    """Both sides of one provider and asset: the lines of the statement, sorted by what
    they are, and Corridor's records of what those lines name."""

    view: _View
    asset: str
    statement: ProviderStatement
    window_start: datetime
    window_end: datetime
    # A deposit the provider received after this moment is too new to repair.
    settled_before: datetime
    arrived: tuple[ProviderTransaction, ...]
    recalled: tuple[ProviderTransaction, ...]
    # Each payout with the withdrawal its reference names, if it names one.
    paid: tuple[tuple[ProviderTransaction, uuid.UUID | None], ...]
    # What the provider charged for a payout, by the payout's id.
    fees: Mapping[str | None, int]
    deposits: Mapping[str, Deposit]
    withdrawals: Mapping[uuid.UUID, Withdrawal]

    def came_and_went(self, line: ProviderTransaction) -> bool:
        """Whether a deposit line is of money the provider received and gave back without
        Corridor ever booking it: the statement shows its return, or the return is all
        that was ever recorded of it here. Only a bank takes a deposit back."""
        deposit = self.deposits.get(line.id)
        if deposit is not None:
            return deposit.entry_id is None and deposit.status == "returned"
        return self.view.kind == "bank" and any(
            recall.related_id == line.id for recall in self.recalled
        )

    def finding(
        self,
        kind: BreakKind,
        provider_ref: str,
        expected: int | None,
        actual: int | None,
        *,
        transaction: ProviderTransaction | None = None,
        returned: bool = False,
        sent: Sent | None = None,
        withdrawal_id: uuid.UUID | None = None,
    ) -> Finding:
        return Finding(
            kind=kind,
            provider=self.view.provider,
            provider_ref=provider_ref,
            asset=self.asset,
            expected=expected,
            actual=actual,
            transaction=transaction,
            returned=returned,
            sent=sent,
            withdrawal_id=withdrawal_id,
        )

    def withdrawal_of(self, withdrawal_id: uuid.UUID | None) -> Withdrawal | None:
        """The withdrawal a payout names, if it is one of this provider's in this asset."""
        withdrawal = self.withdrawals.get(withdrawal_id) if withdrawal_id is not None else None
        if withdrawal is None:
            return None
        if (withdrawal.provider, withdrawal.asset) != (self.view.provider, self.asset):
            return None
        return withdrawal


async def _compare(
    session: AsyncSession,
    view: _View,
    asset: str,
    statement: ProviderStatement,
    window_start: datetime,
    window_end: datetime,
    settled_before: datetime,
) -> list[Finding]:
    """Everything one provider and Corridor disagree about in one asset."""
    lines = statement.transactions
    arrived = tuple(line for line in lines if line.type == _DEPOSIT)
    recalled = tuple(line for line in lines if line.type == _DEPOSIT_RETURN and line.related_id)
    paid = tuple(
        (line, _withdrawal_id(line.reference)) for line in lines if line.type == _PAID[view.kind]
    )
    sides = _Sides(
        view=view,
        asset=asset,
        statement=statement,
        window_start=window_start,
        window_end=window_end,
        settled_before=settled_before,
        arrived=arrived,
        recalled=recalled,
        paid=paid,
        fees={line.related_id: line.amount for line in lines if line.type in _FEES},
        deposits=await payments.find_deposits(
            session,
            view.provider,
            {line.id for line in arrived}
            | {line.related_id for line in recalled if line.related_id is not None},
        ),
        withdrawals=await payments.find_withdrawals(
            session,
            {withdrawal_id for _, withdrawal_id in paid if withdrawal_id is not None}
            | set(view.sent)
            | view.gone,
        ),
    )
    found = await _deposit_findings(session, sides)
    found += await _payout_findings(session, sides)
    balance = await _balance_finding(session, sides)
    if balance is not None:
        found.append(balance)
    return found


async def _deposit_findings(session: AsyncSession, sides: _Sides) -> list[Finding]:
    found: list[Finding] = []
    # Deposits the provider has, against the books.
    for line in sides.arrived:
        if line.occurred_at < sides.window_start:
            continue
        deposit = sides.deposits.get(line.id)
        if deposit is not None and (deposit.asset, deposit.amount) != (sides.asset, line.amount):
            found.append(sides.finding("amount_mismatch", line.id, deposit.amount, line.amount))
        elif deposit is not None and (deposit.entry_id is not None or deposit.status == "returned"):
            # On the books, or recorded as returned before it was ever received: there is
            # nothing here to put right.
            continue
        elif line.occurred_at > sides.settled_before:
            # Too new: its webhook may still arrive, and is the better record of it.
            continue
        else:
            # Not recorded, or recorded and never credited: a row alone moves no money.
            # One the statement also shows as returned is not credited by the repair: the
            # money is no longer at the provider.
            found.append(
                sides.finding(
                    "missing_deposit",
                    line.id,
                    None,
                    line.amount,
                    transaction=line,
                    returned=deposit is None and sides.came_and_went(line),
                )
            )

    # Deposits on the books, against the provider. A recalled deposit is one it knows.
    known = {line.id for line in sides.arrived} | {line.related_id for line in sides.recalled}
    credited = await payments.deposits_credited_between(
        session,
        sides.view.provider,
        sides.asset,
        sides.window_start,
        sides.window_end,
        limit=SCAN_LIMIT,
    )
    for deposit in credited:
        if deposit.provider_ref in known:
            continue
        if await _booked_before(session, deposit, sides.window_start):
            # On the books before this window, and changed in it: released from suspense,
            # say. Its line is on an earlier statement, not on this one.
            continue
        found.append(sides.finding("unknown_deposit", deposit.provider_ref, deposit.amount, None))
    return found


async def _payout_findings(session: AsyncSession, sides: _Sides) -> list[Finding]:
    found: list[Finding] = []
    # What the provider says became of the withdrawals in flight: its own answer for each,
    # and for one it was not asked about, the line of its statement.
    settled = {
        withdrawal_id: sent
        for withdrawal_id, sent in sides.view.sent.items()
        if sent.asset == sides.asset and sent.status != "pending"
    }
    for line, withdrawal_id in sides.paid:
        withdrawal = sides.withdrawal_of(withdrawal_id)
        in_window = line.occurred_at >= sides.window_start
        if withdrawal is None:
            if in_window:
                found.append(sides.finding("unknown_payout", line.id, None, line.amount))
        elif withdrawal.status in _IN_FLIGHT:
            settled.setdefault(
                withdrawal.id,
                Sent(
                    id=line.id,
                    status="completed",
                    asset=sides.asset,
                    amount=line.amount,
                    fee=sides.fees.get(line.id, 0),
                    reference=str(withdrawal.id),
                    settled_at=line.occurred_at,
                    failure_reason=None,
                    tx_hash=line.tx_hash,
                ),
            )
        elif not in_window:
            continue
        elif withdrawal.amount != line.amount:
            found.append(sides.finding("amount_mismatch", line.id, withdrawal.amount, line.amount))
        elif withdrawal.status != "completed":
            # Paid out by the provider after the funds went back to the user.
            found.append(sides.finding("unknown_payout", line.id, None, line.amount))

    for withdrawal_id, sent in settled.items():
        withdrawal = sides.withdrawal_of(withdrawal_id)
        if withdrawal is None or withdrawal.status not in _IN_FLIGHT:
            # Closed while the providers were being read.
            continue
        if withdrawal.amount != sent.amount:
            found.append(sides.finding("amount_mismatch", sent.id, withdrawal.amount, sent.amount))
        else:
            found.append(
                sides.finding(
                    "missing_payout_result",
                    sent.id,
                    withdrawal.amount,
                    sent.amount,
                    sent=sent,
                    withdrawal_id=withdrawal.id,
                )
            )

    # Recorded as sent, and the provider says it has no such payout.
    for withdrawal_id in sides.view.gone:
        withdrawal = sides.withdrawal_of(withdrawal_id)
        if (
            withdrawal is not None
            and withdrawal.status in _IN_FLIGHT
            and withdrawal.provider_ref is not None
        ):
            found.append(
                sides.finding("unknown_payout", withdrawal.provider_ref, withdrawal.amount, None)
            )

    # Payouts settled on the books, against the provider.
    listed = {line.id for line, _ in sides.paid}
    completed = await payments.withdrawals_completed_between(
        session,
        sides.view.provider,
        sides.asset,
        sides.window_start,
        sides.window_end,
        limit=SCAN_LIMIT,
    )
    found.extend(
        sides.finding(
            "unknown_payout", withdrawal.provider_ref or str(withdrawal.id), withdrawal.amount, None
        )
        for withdrawal in completed
        if withdrawal.provider_ref not in listed
    )
    return found


async def _balance_finding(session: AsyncSession, sides: _Sides) -> Finding | None:
    """The settlement account against the provider's balance, as both were at the end of
    the window, allowing for what the provider had booked by then and the ledger had not."""
    account = await ledger.find_account(
        session, _SETTLEMENT[sides.view.kind], sides.asset, provider=sides.view.provider
    )
    booked = await _balance_at(session, account, sides.window_end) if account is not None else 0

    in_transit = 0
    for line in sides.arrived:
        deposit = sides.deposits.get(line.id)
        if deposit is None or deposit.entry_id is None:
            if sides.came_and_went(line):
                # Received and returned at the provider, and never on the books: the two
                # lines cancel there, and there is nothing here for them to differ from.
                continue
            # Received and not credited: reported as missing, if it is in the window. An
            # older one is not allowed for, so that it shows in the balance.
            if line.occurred_at >= sides.window_start:
                in_transit += line.amount
        elif not await _credited_before(session, deposit, sides.window_end):
            in_transit += line.amount
    for line in sides.recalled:
        deposit = sides.deposits.get(line.related_id or "")
        # A return that is not on the books at all is not in transit: it is a difference.
        # Nor is the return of a deposit that was never booked: see above.
        if (
            deposit is not None
            and deposit.entry_id is not None
            and deposit.status == "returned"
            and deposit.updated_at >= sides.window_end
        ):
            in_transit -= line.amount
    # Paid by the provider and still in flight here: by its own answer, which does not
    # depend on how far back the statement was read.
    unsettled: set[uuid.UUID] = set()
    for asked_about, sent in sides.view.sent.items():
        withdrawal = sides.withdrawal_of(asked_about)
        if (
            withdrawal is not None
            and withdrawal.status in _IN_FLIGHT
            and sent.status == "completed"
            and sent.settled_at is not None
            and sent.settled_at < sides.window_end
        ):
            unsettled.add(asked_about)
            in_transit -= sent.amount + sent.fee
    for line, withdrawal_id in sides.paid:
        withdrawal = sides.withdrawal_of(withdrawal_id)
        if withdrawal is None or withdrawal.id in unsettled:
            continue
        # The same, for one the provider was not asked about; or settled after the window.
        if withdrawal.status in _IN_FLIGHT or (
            withdrawal.status == "completed" and withdrawal.updated_at >= sides.window_end
        ):
            in_transit -= line.amount + sides.fees.get(line.id, 0)

    expected = booked + in_transit
    if expected == sides.statement.closing_balance:
        return None
    return sides.finding(
        "settlement_balance", sides.asset, expected, sides.statement.closing_balance
    )


async def _balance_at(session: AsyncSession, account: Account, moment: datetime) -> int:
    """What the ledger had in an account just before ``moment``: its balance now, less
    everything posted to it since."""
    balance = await ledger.get_balance(session, account.id)
    before_seq: int | None = None
    while True:
        lines = await ledger.statement(session, account.id, before_seq=before_seq, limit=_PAGE)
        for line in lines:
            if line.posted_at < moment:
                # Newest first, so everything after this one is older still.
                return balance
            balance -= line.amount if line.direction is account.normal_side else -line.amount
        if len(lines) < _PAGE:
            return balance
        before_seq = lines[-1].seq


async def _credited_before(session: AsyncSession, deposit: Deposit, moment: datetime) -> bool:
    """Whether a deposit that has a journal entry had it before ``moment``."""
    if deposit.status == "returned" and deposit.entry_id is not None:
        # Its row was last changed by the return, so the entry itself is asked.
        return (await ledger.get_entry(session, deposit.entry_id)).posted_at < moment
    return deposit.updated_at < moment


async def _booked_before(session: AsyncSession, deposit: Deposit, moment: datetime) -> bool:
    """Whether the entry that brought a deposit onto the books was posted before ``moment``,
    whatever has happened to its row since."""
    if deposit.entry_id is None:
        return False
    return (await ledger.get_entry(session, deposit.entry_id)).posted_at < moment


def _withdrawal_id(reference: str | None) -> uuid.UUID | None:
    """The withdrawal a provider's payout was made for: Corridor's own id, sent as the
    reference. None for a reference that is not one."""
    try:
        return uuid.UUID(reference) if reference else None
    except ValueError:
        return None
