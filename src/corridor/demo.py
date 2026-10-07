"""The demo: a deposit, a conversion, a transfer and a withdrawal against a running stack.

Everything here is a client. It knows the API and the simulator only by their addresses and
does what a person with two browser tabs could do: call Corridor's public API, and tell the
simulated bank what the outside world did, through the simulator's control endpoints. It
holds no credential of the stack's: not the key Corridor calls its providers with, and no
administrator's. It imports nothing from the rest of Corridor, so
what it shows is what a client sees.

``Stack`` is also what the end-to-end runner drives its scenario through.
"""

import secrets
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

import httpx

REQUEST_TIMEOUT_SECONDS: Final = 15.0
# How long the stack is given to do something that happens behind the API: a webhook to
# arrive and be processed, a payout to settle.
WAIT_SECONDS: Final = 60.0
POLL_SECONDS: Final = 0.2

# Synthetic identifiers of the right shape for the ACH rail. They name nobody's account.
DEMO_ACCOUNT_NUMBER: Final = "000123456789"
DEMO_ROUTING_NUMBER: Final = "021000021"


class DemoError(Exception):
    """The stack did not do what the scenario needs. The message says which step."""


@dataclass(frozen=True, slots=True)
class Person:
    """A registered user, and what authenticates requests as them."""

    name: str
    id: str
    handle: str
    headers: Mapping[str, str] = field(repr=False)


class Stack:
    """A running API and simulator, as a client reaches them."""

    def __init__(
        self,
        base_url: str,
        sim_url: str,
        *,
        sim_control_token: str | None = None,
        wait_seconds: float = WAIT_SECONDS,
    ) -> None:
        self.api = httpx.Client(base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS)
        self.sim = httpx.Client(base_url=sim_url, timeout=REQUEST_TIMEOUT_SECONDS)
        self._sim_control_token = sim_control_token
        self._wait_seconds = wait_seconds

    def close(self) -> None:
        self.api.close()
        self.sim.close()

    # --- people --------------------------------------------------------------------------

    def register(self, name: str) -> Person:
        """Register a user and log them in. The handle and the address are unlike any used
        before, so the demo can be run again against the same stack."""
        handle = f"{name.split()[0].lower()}_{secrets.token_hex(4)}"
        # Made here and kept only in memory: it protects an account that holds play money.
        password = secrets.token_urlsafe(18)
        email = f"{handle}@example.com"
        registered = self.api.post(
            "/v1/auth/register",
            json={"email": email, "handle": handle, "display_name": name, "password": password},
        )
        user = expect(registered, 201, f"register {name}")
        login = self.api.post("/v1/auth/login", json={"email": email, "password": password})
        tokens = expect(login, 200, f"log {name} in")
        return Person(
            name=name,
            id=str(user["id"]),
            handle=handle,
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )

    # --- reading -------------------------------------------------------------------------

    def wallets(self, person: Person) -> dict[str, dict[str, str]]:
        """The person's wallets by asset, each with ``available``, ``held`` and ``total``."""
        response = self.api.get("/v1/wallets", headers=person.headers)
        listed = expect(response, 200, f"read the wallets of {person.name}")["wallets"]
        return {wallet["asset"]: wallet for wallet in listed}

    def available(self, person: Person, asset: str = "USD") -> Decimal:
        wallet = self.wallets(person).get(asset)
        return Decimal(wallet["available"]) if wallet is not None else Decimal(0)

    def wait_for_available(self, person: Person, asset: str, amount: Decimal) -> None:
        self.wait_until(
            f"{person.name} to have {amount} {asset} available",
            lambda: self.available(person, asset) == amount or None,
        )

    def wait_until[T](self, what: str, probe: Callable[[], T | None]) -> T:
        """Ask until ``probe`` has an answer. What happens behind the API takes a moment:
        a webhook is delivered, the worker picks its event up, a payout settles."""
        deadline = time.monotonic() + self._wait_seconds
        while True:
            found = probe()
            if found is not None:
                return found
            if time.monotonic() >= deadline:
                raise DemoError(f"waited {self._wait_seconds:.0f} seconds for {what}")
            time.sleep(POLL_SECONDS)

    # --- what the outside world does -------------------------------------------------------

    def control(
        self, method: str, path: str, body: object = None, *, status: int = 200
    ) -> dict[str, Any]:
        """Call one of the simulator's control endpoints."""
        headers = (
            {"Authorization": f"Bearer {self._sim_control_token}"}
            if self._sim_control_token is not None
            else {}
        )
        response = self.sim.request(method, f"/_control{path}", json=body, headers=headers)
        return expect(response, status, f"simulator {method} {path}")

    def deposit_instruction(self, person: Person, asset: str = "USD") -> dict[str, Any]:
        response = self.api.get(
            "/v1/deposit-instructions", params={"asset": asset}, headers=person.headers
        )
        return expect(response, 200, f"ask where {person.name} deposits {asset}")

    def bank_deposit(self, person: Person, amount: str, asset: str = "USD") -> str:
        """A bank transfer to the person's virtual account arrives. Returns the bank's id.

        The control endpoint names the account by the bank's own id for it, which Corridor's
        API does not show a client. The simulator's control endpoints say which account it
        issued to a customer, so nothing here needs the key Corridor calls the bank with.
        """
        self.deposit_instruction(person, asset)
        issued = self.control("GET", f"/bank/virtual-accounts?customer_reference={person.id}")[
            "virtual_accounts"
        ]
        accounts = [account for account in issued if account["asset"] == asset]
        if len(accounts) != 1:
            raise DemoError(f"could not find the {asset} account the bank issued {person.name}")
        deposit = self.control(
            "POST",
            "/bank/deposits",
            {
                "virtual_account_id": accounts[0]["id"],
                "amount": amount,
                "sender_name": person.name,
                "reference": f"DEMO-{secrets.token_hex(3).upper()}",
            },
            status=201,
        )
        return str(deposit["id"])

    # --- what a person does ----------------------------------------------------------------

    def quote(self, person: Person, sell_asset: str, buy_asset: str, amount: str) -> dict[str, Any]:
        response = self.api.post(
            "/v1/fx/quotes",
            json={"sell_asset": sell_asset, "buy_asset": buy_asset, "sell_amount": amount},
            headers=person.headers,
        )
        return expect(response, 201, f"quote {amount} {sell_asset} into {buy_asset}")

    def convert(self, person: Person, quote_id: str) -> dict[str, Any]:
        response = self.api.post(
            "/v1/fx/conversions", json={"quote_id": quote_id}, headers=_once(person.headers)
        )
        return expect(response, 201, "convert at the quoted rate")

    def transfer(
        self, headers: Mapping[str, str], recipient: Person, amount: str, asset: str = "USD"
    ) -> httpx.Response:
        """Ask for a transfer with these credentials: a person's, or an agent's key. The
        response is returned as it is, because an agent may be answered with a request for
        its owner's approval and no transfer."""
        return self.api.post(
            "/v1/transfers",
            json={"recipient": recipient.id, "asset": asset, "amount": amount},
            headers=_once(headers),
        )

    def add_beneficiary(self, person: Person, asset: str = "USD") -> str:
        response = self.api.post(
            "/v1/beneficiaries",
            json={
                "asset": asset,
                "holder_name": person.name,
                "account_number": DEMO_ACCOUNT_NUMBER,
                "routing_number": DEMO_ROUTING_NUMBER,
            },
            headers=_once(person.headers),
        )
        return str(expect(response, 201, f"save a bank account for {person.name}")["id"])

    def withdraw(
        self, person: Person, beneficiary_id: str, amount: str, asset: str = "USD"
    ) -> dict[str, Any]:
        response = self.api.post(
            "/v1/withdrawals",
            json={"asset": asset, "amount": amount, "beneficiary_id": beneficiary_id},
            headers=_once(person.headers),
        )
        # Accepted, not done: the money is held here and the worker asks the bank to pay.
        return expect(response, 202, f"withdraw {amount} {asset}")

    def wait_for_withdrawal(self, person: Person, withdrawal_id: str) -> dict[str, Any]:
        """Wait until a withdrawal has ended, one way or the other, and return it."""

        def ended() -> dict[str, Any] | None:
            response = self.api.get(f"/v1/withdrawals/{withdrawal_id}", headers=person.headers)
            withdrawal = expect(response, 200, "read the withdrawal")
            return withdrawal if withdrawal["status"] in ("completed", "failed") else None

        return self.wait_until("the withdrawal to be settled by the bank", ended)


def expect(response: httpx.Response, status: int, step: str) -> dict[str, Any]:
    """The body of a response with the status the step needs, or a refusal that says what
    came back instead."""
    if response.status_code != status:
        raise DemoError(_refusal(response, step))
    body = response.json()
    if not isinstance(body, dict):
        raise DemoError(f"could not {step}: the answer was not a JSON object")
    return body


def _refusal(response: httpx.Response, step: str) -> str:
    try:
        body = response.json()
    except ValueError:
        body = {}
    said = body if isinstance(body, dict) else {}
    # A problem document names its refusal in ``code``; the simulator nests one in ``error``.
    nested = said.get("error")
    code = said.get("code") or (nested.get("code") if isinstance(nested, dict) else None)
    code = code or "no code"
    return f"could not {step}: HTTP {response.status_code} ({code})"


def _once(headers: Mapping[str, str]) -> dict[str, str]:
    """The headers with an idempotency key that no other request has."""
    return {**headers, "Idempotency-Key": f"demo-{uuid.uuid4()}"}


def _balances(stack: Stack, *people: Person) -> None:
    for person in people:
        held = [
            f"{wallet['available']} {asset}"
            + (f" (and {wallet['held']} {asset} on hold)" if Decimal(wallet["held"]) else "")
            for asset, wallet in sorted(stack.wallets(person).items())
            if Decimal(wallet["total"])
        ]
        print(f"      {person.name} has {', '.join(held) if held else 'nothing yet'}.")


def run(stack: Stack) -> None:
    """Tell the story, one step at a time, with the balances after each."""
    print("1. Two people open accounts.")
    ana = stack.register("Ana Lima")
    bruno = stack.register("Bruno Costa")
    print(f"      Ana is @{ana.handle} and Bruno is @{bruno.handle}.")
    _balances(stack, ana, bruno)

    print("2. Ana asks where to send US dollars, and her bank sends 500.00 USD there.")
    details = stack.deposit_instruction(ana)["details"]
    print(f"      Corridor gave her an account on the {details['rail'].upper()} rail.")
    stack.bank_deposit(ana, "500.00")
    print("      The bank tells Corridor by webhook, and the worker credits her wallet.")
    stack.wait_for_available(ana, "USD", Decimal("500.00"))
    _balances(stack, ana, bruno)

    print("3. Ana converts 100.00 USD into Mexican pesos at a quoted rate.")
    quote = stack.quote(ana, "USD", "MXN", "100.00")
    print(f"      The quote: {quote['sell_amount']} USD buys {quote['buy_amount']} MXN.")
    stack.convert(ana, quote["id"])
    _balances(stack, ana, bruno)

    print("4. Ana sends Bruno 50.00 USD. It arrives at once: both are Corridor users.")
    sent = expect(stack.transfer(ana.headers, bruno, "50.00"), 201, "send Bruno 50.00 USD")
    print(f"      The fee was {sent['fee']} USD.")
    _balances(stack, ana, bruno)

    print("5. Bruno saves his bank account and withdraws 40.00 USD to it.")
    beneficiary_id = stack.add_beneficiary(bruno)
    withdrawal = stack.withdraw(bruno, beneficiary_id, "40.00")
    print(
        f"      Corridor holds {withdrawal['amount']} USD and a fee of {withdrawal['fee']} USD"
        " while the bank pays out."
    )
    _balances(stack, ana, bruno)
    settled = stack.wait_for_withdrawal(bruno, withdrawal["id"])
    if settled["status"] != "completed":
        raise DemoError(f"the withdrawal ended as {settled['status']}")
    print("      The bank says the payout settled, and the hold becomes a payment.")
    _balances(stack, ana, bruno)
