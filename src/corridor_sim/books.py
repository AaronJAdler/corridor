"""A provider's own books.

Each asset has one account. Its balance changes only by posting a transaction to it, so the
running balance and the statement cannot drift apart: the statement is the list of
everything that ever changed the balance.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from corridor_sim import money
from corridor_sim.clock import format_time

Direction = Literal["credit", "debit"]


@dataclass(frozen=True, slots=True)
class Transaction:
    """One settled movement: a line of the statement."""

    id: str
    type: str
    direction: Direction
    asset: str
    amount: Decimal
    reference: str | None
    related_id: str | None
    occurred_at: datetime
    # On-chain movements carry the hash of the transaction that made them.
    tx_hash: str | None = None

    @property
    def signed_amount(self) -> Decimal:
        if self.direction == "credit":
            return self.amount
        return money.subtract(money.ZERO, self.amount)


@dataclass(frozen=True, slots=True)
class Statement:
    asset: str
    start: datetime
    end: datetime
    transactions: tuple[Transaction, ...]
    closing_balance: Decimal


class Account:
    """What the provider holds for Corridor in one asset. It may be overdrawn."""

    def __init__(self, asset: str) -> None:
        self.asset = asset
        self.balance = money.ZERO
        self._transactions: list[Transaction] = []

    def post(self, transaction: Transaction) -> None:
        if transaction.asset != self.asset:
            raise ValueError(f"a {transaction.asset} transaction posted to {self.asset}")
        if transaction.amount <= money.ZERO:
            raise ValueError("a transaction moves a positive amount")
        self.balance = money.add(self.balance, transaction.signed_amount)
        self._transactions.append(transaction)

    def statement(self, start: datetime, end: datetime) -> Statement:
        """The movements in ``[start, end)`` and the balance at ``end``.

        The closing balance is added up from the transactions rather than read from the
        running balance: it is the balance at the end of the window, not the balance now.
        """
        return Statement(
            asset=self.asset,
            start=start,
            end=end,
            transactions=tuple(
                transaction
                for transaction in self._transactions
                if start <= transaction.occurred_at < end
            ),
            closing_balance=money.total(
                transaction.signed_amount
                for transaction in self._transactions
                if transaction.occurred_at < end
            ),
        )


def statement_document(statement: Statement, *, with_tx_hash: bool = False) -> dict[str, object]:
    return {
        "asset": statement.asset,
        "from": format_time(statement.start),
        "to": format_time(statement.end),
        "transactions": [
            _transaction_document(transaction, with_tx_hash=with_tx_hash)
            for transaction in statement.transactions
        ],
        "closing_balance": money.format_amount(statement.closing_balance, statement.asset),
    }


def _transaction_document(transaction: Transaction, *, with_tx_hash: bool) -> dict[str, object]:
    document: dict[str, object] = {
        "id": transaction.id,
        "type": transaction.type,
        "direction": transaction.direction,
        "asset": transaction.asset,
        "amount": money.format_amount(transaction.amount, transaction.asset),
        "reference": transaction.reference,
        "related_id": transaction.related_id,
        "occurred_at": format_time(transaction.occurred_at),
    }
    if with_tx_hash:
        document["tx_hash"] = transaction.tx_hash
    return document
