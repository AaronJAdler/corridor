"""The principal: who a request acts for, and what it may do."""

import dataclasses

import pytest

from corridor.identity import (
    InsufficientScope,
    Principal,
    Scope,
    require_admin,
    require_scope,
    require_user_session,
)
from corridor.platform.errors import PermissionDenied
from corridor.platform.ids import new_id

OWNER = new_id()
SESSION = new_id()


def user(role: str = "user") -> Principal:
    return Principal.for_user(OWNER, role, SESSION)  # type: ignore[arg-type]


def agent(*scopes: str, owner_role: str = "user") -> Principal:
    """An agent acting for OWNER with the scopes its key was given."""
    return Principal(
        user_id=OWNER,
        actor_type="agent",
        actor_id=new_id(),
        role=owner_role,  # type: ignore[arg-type]
        scopes=frozenset(scopes),
        session_id=None,
    )


def test_a_users_own_session_acts_as_the_user_with_every_scope() -> None:
    principal = Principal.for_user(OWNER, "user", SESSION)

    assert principal == Principal(
        user_id=OWNER,
        actor_type="user",
        actor_id=OWNER,
        role="user",
        scopes=frozenset({"*"}),
        session_id=SESSION,
    )
    assert (principal.is_agent, principal.is_admin) == (False, False)
    assert all(principal.has_scope(scope) for scope in Scope)
    assert principal.has_scope("a:scope-added-later")


def test_an_agent_has_only_the_scopes_it_was_given() -> None:
    principal = agent(Scope.WALLET_READ, Scope.TRANSFERS_CREATE)

    assert principal.is_agent is True
    assert {scope for scope in Scope if principal.has_scope(scope)} == {
        Scope.WALLET_READ,
        Scope.TRANSFERS_CREATE,
    }
    assert principal.has_scope("wallet:read") is True
    assert principal.has_scope("*") is False
    assert agent().has_scope(Scope.WALLET_READ) is False


def test_the_scope_names_are_the_documented_ones() -> None:
    # API keys store these strings, so renaming one silently strips it from every key.
    assert {scope.name: scope.value for scope in Scope} == {
        "WALLET_READ": "wallet:read",
        "TRANSFERS_READ": "transfers:read",
        "TRANSFERS_CREATE": "transfers:create",
        "DEPOSITS_READ": "deposits:read",
        "WITHDRAWALS_READ": "withdrawals:read",
        "WITHDRAWALS_CREATE": "withdrawals:create",
        "BENEFICIARIES_READ": "beneficiaries:read",
        "BENEFICIARIES_WRITE": "beneficiaries:write",
        "FX_READ": "fx:read",
        "FX_CONVERT": "fx:convert",
    }


def test_a_principal_cannot_be_changed() -> None:
    principal = user()

    with pytest.raises(dataclasses.FrozenInstanceError):
        principal.role = "admin"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        principal.scopes = frozenset({"*"})  # type: ignore[misc]


def test_requiring_a_scope_passes_those_who_have_it() -> None:
    require_scope(user(), Scope.TRANSFERS_CREATE)
    require_scope(agent(Scope.TRANSFERS_CREATE), Scope.TRANSFERS_CREATE)


@pytest.mark.parametrize(
    "principal", [agent(), agent(Scope.TRANSFERS_READ), agent(Scope.WALLET_READ, Scope.FX_READ)]
)
def test_requiring_a_scope_refuses_a_credential_without_it(principal: Principal) -> None:
    with pytest.raises(InsufficientScope) as refusal:
        require_scope(principal, Scope.TRANSFERS_CREATE)

    assert (refusal.value.status, refusal.value.code) == (403, "insufficient_scope")
    assert isinstance(refusal.value, PermissionDenied)


def test_an_action_that_is_the_owners_alone_passes_a_user_session() -> None:
    require_user_session(user())
    require_user_session(user("admin"))


def test_an_action_that_is_the_owners_alone_refuses_an_agent_whatever_its_scopes() -> None:
    for principal in (agent(), agent(*Scope), agent("*")):
        with pytest.raises(InsufficientScope) as refusal:
            require_user_session(principal)

        assert (refusal.value.status, refusal.value.code) == (403, "insufficient_scope")


def test_an_admin_passes_the_admin_check() -> None:
    admin = user("admin")

    assert admin.is_admin is True
    require_admin(admin)


def test_a_user_who_is_not_an_admin_is_refused() -> None:
    with pytest.raises(PermissionDenied) as refusal:
        require_admin(user())

    assert (refusal.value.status, refusal.value.code) == (403, "permission_denied")
    assert not isinstance(refusal.value, InsufficientScope)


def test_an_agent_is_never_an_admin_whatever_its_owner_is() -> None:
    for principal in (agent(owner_role="admin"), agent("*", owner_role="admin")):
        assert principal.is_admin is False
        with pytest.raises(PermissionDenied):
            require_admin(principal)
