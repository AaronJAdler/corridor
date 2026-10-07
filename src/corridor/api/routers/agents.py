"""Agent endpoints: a user creates agents, gives them keys, and stops them.

All of it is the owner's alone to do, from their own session. An agent's key is refused on
every route here, whatever its scopes, so no agent can make itself a key, widen one or
bring itself back.
"""

import uuid
from datetime import datetime
from typing import Annotated, Self

from fastapi import APIRouter, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import agents
from corridor.api.deps import CurrentPrincipal, Db, SettingsDep
from corridor.api.schemas import DisplayName, Text
from corridor.platform.logging import get_logger
from corridor.platform.pagination import DEFAULT_LIMIT, Page

log = get_logger(__name__)

router = APIRouter(prefix="/v1/agents", tags=["agents"])

# Far above the number of scopes there are. These bound what a request can make the
# server read; which scopes a key may hold is the agents module's to say.
_MAX_SCOPES = 32
_MAX_SCOPE_LENGTH = 64


class _Request(BaseModel):
    # An unknown field is refused rather than dropped: a client that misspells one learns
    # of it at once.
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentRequest(_Request):
    name: DisplayName


class KeyRequest(_Request):
    scopes: Annotated[
        list[Annotated[Text, Field(max_length=_MAX_SCOPE_LENGTH)]], Field(max_length=_MAX_SCOPES)
    ]
    # Left out, the key works until it is revoked. A time must say which timezone it is in.
    expires_at: AwareDatetime | None = None


class KeyResponse(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    # The public part of the key, to recognise it by.
    prefix: str
    scopes: list[str]
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, key: agents.AgentKey) -> Self:
        return cls(
            id=key.id,
            agent_id=key.agent_id,
            prefix=key.prefix,
            scopes=list(key.scopes),
            expires_at=key.expires_at,
            revoked_at=key.revoked_at,
            last_used_at=key.last_used_at,
            created_at=key.created_at,
        )


class IssuedKeyResponse(KeyResponse):
    # The key itself. This response is the only place it ever appears.
    key: str

    @classmethod
    def issued(cls, issued: agents.IssuedKey) -> Self:
        return cls(**KeyResponse.of(issued.details).model_dump(), key=issued.key)


class AgentResponse(BaseModel):
    id: uuid.UUID
    name: str
    status: agents.AgentStatus
    created_at: datetime
    keys: list[KeyResponse]

    @classmethod
    def of(cls, agent: agents.Agent) -> Self:
        return cls(
            id=agent.id,
            name=agent.name,
            status=agent.status,
            created_at=agent.created_at,
            keys=[KeyResponse.of(key) for key in agent.keys],
        )


class AgentPageResponse(BaseModel):
    items: list[AgentResponse]
    # Send it back as ``cursor`` for the next page. Null on the last page.
    next_cursor: str | None


@router.post("", status_code=201, summary="Create an agent")
async def create_agent(body: AgentRequest, principal: CurrentPrincipal, db: Db) -> AgentResponse:
    agent = await db.run(lambda session: agents.create_agent(session, principal, name=body.name))
    log.info("agent.created", agent_id=str(agent.id))
    return AgentResponse.of(agent)


@router.get("", summary="The user's agents, newest first, each with its keys")
async def list_agents(
    principal: CurrentPrincipal,
    db: Db,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> AgentPageResponse:
    async def work(session: AsyncSession) -> Page[agents.Agent]:
        return await agents.list_agents(session, principal, cursor=cursor, limit=limit)

    page = await db.run(work)
    return AgentPageResponse(
        items=[AgentResponse.of(agent) for agent in page.items], next_cursor=page.next_cursor
    )


@router.post("/{agent_id}/pause", summary="Stop an agent's keys from working, for now")
async def pause_agent(agent_id: uuid.UUID, principal: CurrentPrincipal, db: Db) -> AgentResponse:
    agent = await db.run(lambda session: agents.pause_agent(session, principal, agent_id))
    log.info("agent.paused", agent_id=str(agent.id))
    return AgentResponse.of(agent)


@router.post("/{agent_id}/resume", summary="Let a paused agent's keys work again")
async def resume_agent(agent_id: uuid.UUID, principal: CurrentPrincipal, db: Db) -> AgentResponse:
    agent = await db.run(lambda session: agents.resume_agent(session, principal, agent_id))
    log.info("agent.resumed", agent_id=str(agent.id))
    return AgentResponse.of(agent)


@router.post("/{agent_id}/revoke", summary="Stop an agent for good")
async def revoke_agent(agent_id: uuid.UUID, principal: CurrentPrincipal, db: Db) -> AgentResponse:
    agent = await db.run(lambda session: agents.revoke_agent(session, principal, agent_id))
    log.info("agent.revoked", agent_id=str(agent.id))
    return AgentResponse.of(agent)


@router.post(
    "/{agent_id}/keys", status_code=201, summary="Give an agent a key, which is shown this once"
)
async def create_key(
    agent_id: uuid.UUID,
    body: KeyRequest,
    response: Response,
    principal: CurrentPrincipal,
    db: Db,
    settings: SettingsDep,
) -> IssuedKeyResponse:
    async def work(session: AsyncSession) -> agents.IssuedKey:
        return await agents.issue_key(
            session,
            principal,
            agent_id,
            scopes=body.scopes,
            expires_at=body.expires_at,
            settings=settings,
        )

    issued = await db.run(work)
    # Its id and never the key: this line is kept far longer than the response is.
    log.info("agent.key_created", agent_id=str(agent_id), key_id=str(issued.details.id))
    # The key is for the client that asked and for nobody on the way.
    response.headers["Cache-Control"] = "no-store"
    return IssuedKeyResponse.issued(issued)


@router.delete(
    "/{agent_id}/keys/{key_id}", status_code=204, summary="Stop one key from working, for good"
)
async def revoke_key(
    agent_id: uuid.UUID, key_id: uuid.UUID, principal: CurrentPrincipal, db: Db
) -> None:
    await db.run(lambda session: agents.revoke_key(session, principal, agent_id, key_id))
    log.info("agent.key_revoked", agent_id=str(agent_id), key_id=str(key_id))
