"""Payments: money moving between users.

Other modules use the names exported here and nothing else from this package.
"""

from corridor.payments.errors import (
    MAX_MEMO_LENGTH,
    CannotTransferToSelf,
    DuplicateTransfer,
    InvalidMemo,
    RecipientNotFound,
    TransferNotFound,
)
from corridor.payments.transfers import create_transfer, get_transfer, list_transfers
from corridor.payments.types import Transfer, TransferStatus

__all__ = [
    "MAX_MEMO_LENGTH",
    "CannotTransferToSelf",
    "DuplicateTransfer",
    "InvalidMemo",
    "RecipientNotFound",
    "Transfer",
    "TransferNotFound",
    "TransferStatus",
    "create_transfer",
    "get_transfer",
    "list_transfers",
]
