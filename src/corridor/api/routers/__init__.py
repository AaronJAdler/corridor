"""The routers the API serves, in the order the app includes them."""

from fastapi import APIRouter

from corridor.api.routers import (
    auth,
    beneficiaries,
    deposits,
    fx,
    transfers,
    wallets,
    webhooks,
    withdrawals,
)

# A user is registered together with their wallets, in the one transaction.
auth.on_user_registered.append(wallets.provision_for_new_user)

ROUTERS: tuple[APIRouter, ...] = (
    auth.router,
    auth.account_router,
    wallets.router,
    transfers.router,
    deposits.instructions_router,
    deposits.router,
    beneficiaries.router,
    withdrawals.router,
    fx.router,
    webhooks.router,
)
