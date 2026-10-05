"""FastAPI dependencies shared by the routers."""

from typing import Annotated, cast

from fastapi import Depends, Request

from corridor.api.container import Container
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.redis import RedisStore


def get_container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


def get_settings(container: Annotated[Container, Depends(get_container)]) -> Settings:
    return container.settings


def get_db(container: Annotated[Container, Depends(get_container)]) -> Database:
    return container.db


def get_redis(container: Annotated[Container, Depends(get_container)]) -> RedisStore:
    return container.redis


SettingsDep = Annotated[Settings, Depends(get_settings)]
Db = Annotated[Database, Depends(get_db)]
Redis = Annotated[RedisStore, Depends(get_redis)]
