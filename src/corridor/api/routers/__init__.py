"""The routers the API serves, in the order the app includes them."""

from fastapi import APIRouter

from corridor.api.routers import auth

ROUTERS: tuple[APIRouter, ...] = (auth.router, auth.account_router)
