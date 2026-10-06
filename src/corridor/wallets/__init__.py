"""Wallets: a user's accounts per asset, their balances and their statements.

Other modules use the names exported here and nothing else from this package.
"""

from corridor.wallets.errors import WalletNotFound
from corridor.wallets.service import get_wallet, get_wallets, owner_of, provision, resolve
from corridor.wallets.statements import list_entries
from corridor.wallets.types import StatementEntry, Wallet, WalletBalance

__all__ = [
    "StatementEntry",
    "Wallet",
    "WalletBalance",
    "WalletNotFound",
    "get_wallet",
    "get_wallets",
    "list_entries",
    "owner_of",
    "provision",
    "resolve",
]
