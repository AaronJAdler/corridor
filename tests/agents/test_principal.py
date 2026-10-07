"""The principal of an agent: whose money, who is acting, and what was allowed."""

from corridor.identity import Principal, Scope
from corridor.platform.ids import new_id

OWNER = new_id()
AGENT = new_id()


def test_an_agents_principal_acts_for_its_owner_with_the_scopes_given_and_no_session() -> None:
    principal = Principal.for_agent(OWNER, AGENT, [Scope.WALLET_READ, Scope.FX_READ])

    assert principal == Principal(
        user_id=OWNER,
        actor_type="agent",
        actor_id=AGENT,
        role="user",
        scopes=frozenset({"wallet:read", "fx:read"}),
        session_id=None,
    )
    assert (principal.is_agent, principal.is_admin) == (True, False)
    assert principal.agent_id == AGENT


def test_no_agent_holds_every_scope_whatever_it_is_built_with() -> None:
    principal = Principal.for_agent(OWNER, AGENT, ["*", Scope.WALLET_READ])

    assert principal.scopes == frozenset({"wallet:read"})
    assert not principal.has_scope(Scope.TRANSFERS_CREATE)
    assert not principal.has_scope("a:scope-added-later")


def test_an_agent_with_no_scopes_can_do_nothing() -> None:
    principal = Principal.for_agent(OWNER, AGENT, [])

    assert [scope for scope in Scope if principal.has_scope(scope)] == []


def test_a_user_acting_for_themselves_has_no_agent_id() -> None:
    principal = Principal.for_user(OWNER, "admin", new_id())

    assert principal.agent_id is None
    assert principal.actor_id == OWNER
