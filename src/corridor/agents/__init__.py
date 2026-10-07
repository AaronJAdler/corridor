"""Agents: the principals a user lets act on their wallet, and the keys they act with.

Other modules use the names exported here and nothing else from this package.
"""

from corridor.agents.errors import (
    AgentKeyNotFound,
    AgentKeysUnavailable,
    AgentNotFound,
    AgentRevoked,
    InvalidExpiry,
    InvalidScopes,
)
from corridor.agents.keys import AGENT_SCOPES
from corridor.agents.keys import read as read_key
from corridor.agents.service import (
    authenticate,
    create_agent,
    issue_key,
    list_agents,
    pause_agent,
    resume_agent,
    revoke_agent,
    revoke_key,
)
from corridor.agents.types import (
    Agent,
    AgentKey,
    AgentStatus,
    IssuedKey,
    KeyOutcome,
    KeyRefusal,
    PresentedKey,
)

__all__ = [
    "AGENT_SCOPES",
    "Agent",
    "AgentKey",
    "AgentKeyNotFound",
    "AgentKeysUnavailable",
    "AgentNotFound",
    "AgentRevoked",
    "AgentStatus",
    "InvalidExpiry",
    "InvalidScopes",
    "IssuedKey",
    "KeyOutcome",
    "KeyRefusal",
    "PresentedKey",
    "authenticate",
    "create_agent",
    "issue_key",
    "list_agents",
    "pause_agent",
    "read_key",
    "resume_agent",
    "revoke_agent",
    "revoke_key",
]
