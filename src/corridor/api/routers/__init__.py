"""The routers the API serves, in the order the app includes them."""

from fastapi import APIRouter

from corridor.api.routers import (
    admin_kyc,
    admin_ops,
    admin_recon,
    admin_reviews,
    admin_risk,
    admin_users,
    agents,
    approvals,
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
    agents.router,
    approvals.router,
    webhooks.router,
    admin_kyc.router,
    admin_recon.router,
    admin_ops.router,
    admin_reviews.router,
    admin_risk.router,
    admin_users.router,
)
