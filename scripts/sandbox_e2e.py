"""End to end against Increase's sandbox: the API and the worker as real processes, with the
bank rail set to ``increase_sandbox``, and money moved through it by Increase's simulations.

Run by hand, never in CI: by the ``Sandbox end to end`` workflow, which holds the key in the
``sandbox`` environment, or locally with the key in the environment. What it needs:

    CORRIDOR_INCREASE_API_KEY          the sandbox API key. Read from the environment only,
                                       never printed, and handed to the two processes and
                                       to the simulation calls this script makes
    CORRIDOR_INCREASE_ACCOUNT_ID       optional: without it the run uses the one open account
                                       the key reaches, opens one if there is none, and stops
                                       if there are several
    CORRIDOR_TEST_POSTGRES_ADMIN_URL   as for the test suite: a scratch database is made
    CORRIDOR_TEST_REDIS_URL            and dropped again
    CORRIDOR_SANDBOX_TUNNEL            optional: the path of a cloudflared binary. With it,
                                       the run proves Increase's webhooks instead (below)

With CORRIDOR_SANDBOX_TUNNEL, the API is reached through a Cloudflare quick tunnel, which
needs no account and lasts as long as the run. The run registers an Event Subscription for
the tunnel's address with a shared secret of its own, turns the worker's listing of events
off, and runs the same scenario: every event then arrives by webhook, verified by its
signature, or the scenario does not finish. The subscription is deleted at the end.

No webhook can reach a run like this, so events come in the way such a run gets them: the
worker lists them from Increase's Events API. The scenario:

1. A user asks where to send dollars, and is given an Increase account number.
2. Increase simulates an inbound ACH credit to it; the user's balance shows it.
3. The user saves a bank account and withdraws to it; Increase simulates the ACH transfer
   settling; the withdrawal completes.
4. The user withdraws again; Increase simulates the transfer being returned; the
   withdrawal fails and the funds come back.
5. The ledger verifies, and reconciliation finds nothing wrong with this run's money.

The sandbox account outlives the run, so reconciliation also sees what earlier runs left
there: those findings are printed, and only one about this run's own deposits or payouts
fails it.

Each step also prints what Increase answered where ADR 0020 says the sandbox has to confirm
a reading, so that a run settles them.
"""

import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# The project root, for the test suite's database helpers, and this directory, for the
# ordinary end-to-end run's processes and checks.
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from tests.support import postgres  # noqa: E402 - needs the path above
from tests.support import redis as redis_support  # noqa: E402 - needs the path above

from corridor.demo import DemoError, Person, Stack  # noqa: E402 - needs the path above
from corridor.identity import generate_private_key_pem  # noqa: E402 - needs the path above
from corridor.platform.config import INCREASE_SANDBOX_URL  # noqa: E402 - needs the path above
from e2e import ROOT, Processes, check, free_port  # noqa: E402 - needs the path above

# Things happen at the sandbox's pace: the worker lists events every ten seconds, and each
# one is read back from Increase before it is applied.
WAIT_SECONDS = 180.0
POLL_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 30.0
# Reconciliation runs often and looks back over a short window, so that it reads this run
# and as little as possible of earlier ones.
RECONCILIATION_INTERVAL_SECONDS = 10
RECONCILIATION_WINDOW_SECONDS = 1800
RECONCILIATION_GRACE_SECONDS = 1
# The categories the worker routes, which the run's Event Subscription asks for.
ROUTED_CATEGORIES = (
    "ach_transfer.created",
    "ach_transfer.updated",
    "inbound_ach_transfer.created",
    "inbound_ach_transfer.updated",
)
TUNNEL_ADDRESS = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
TUNNEL_SECONDS = 90.0
# The name of the account a run opens in a sandbox that has none.
SANDBOX_ACCOUNT_NAME = "Corridor sandbox"
# Never contacted: the demo client wants a simulator's address, and this run has none.
NO_SIMULATOR = "http://127.0.0.1:9"


class SandboxError(Exception):
    """A step against the sandbox did not go as the scenario needs."""


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SandboxError(f"{name} is not set")
    return value


class Increase:
    """The sandbox's API, as far as this script drives it: the simulations, and reads to
    find the objects Corridor made. The key is held here and nowhere it is printed."""

    def __init__(self, key: str) -> None:
        self._http = httpx.Client(
            base_url=INCREASE_SANDBOX_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    def close(self) -> None:
        self._http.close()

    def call(self, method: str, path: str, **options: Any) -> dict[str, Any]:
        response = self._http.request(method, path, **options)
        if response.status_code != 200:
            # Increase's error title and type, never the request.
            try:
                problem = response.json()
                said = f"{problem.get('type')}: {problem.get('title')}"
            except ValueError:
                said = "no JSON body"
            raise SandboxError(
                f"Increase answered {method} {path} with {response.status_code}, {said}"
            )
        body = response.json()
        if not isinstance(body, dict):
            raise SandboxError(f"Increase answered {method} {path} with something not an object")
        return body

    def account(self) -> str:
        """The account this run moves money through.

        A sandbox starts with no account, so a run opens one when there is none. With
        several, it does not guess: it names them and stops, for the variable to say which.
        """
        listed = self.call("GET", "/accounts", params={"status.in": "open", "limit": "100"})["data"]
        open_usd = [found for found in listed if found.get("currency") == "USD"]
        if not open_usd:
            opened = self.call("POST", "/accounts", json={"name": SANDBOX_ACCOUNT_NAME})
            print(f"  note  the sandbox had no open account; opened {opened['id']}")
            return str(opened["id"])
        if len(open_usd) > 1:
            ids = ", ".join(str(found["id"]) for found in open_usd)
            raise SandboxError(
                f"the key reaches {len(open_usd)} open accounts ({ids}); "
                "set CORRIDOR_INCREASE_ACCOUNT_ID to the one to use"
            )
        return str(open_usd[0]["id"])

    def subscribe(self, url: str, shared_secret: str) -> str:
        """An Event Subscription for this run's webhook address, signed with its secret."""
        created = self.call(
            "POST",
            "/event_subscriptions",
            json={
                "url": url,
                "shared_secret": shared_secret,
                "selected_event_categories": [
                    {"event_category": category} for category in ROUTED_CATEGORIES
                ],
            },
        )
        return str(created["id"])

    def unsubscribe(self, subscription_id: str) -> None:
        self.call("PATCH", f"/event_subscriptions/{subscription_id}", json={"status": "deleted"})

    def account_number_id(self, number: str, since: datetime) -> str:
        listed = self.call(
            "GET",
            "/account_numbers",
            params={"created_at.on_or_after": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": "100"},
        )["data"]
        matches = [found["id"] for found in listed if found.get("account_number") == number]
        if len(matches) != 1:
            raise SandboxError(f"found {len(matches)} account numbers made by this run")
        return str(matches[0])

    def transfer_for(self, withdrawal_id: str) -> dict[str, Any] | None:
        listed = self.call(
            "GET", "/ach_transfers", params={"idempotency_key": withdrawal_id, "limit": "10"}
        )["data"]
        return listed[0] if listed else None


def wait_for[T](what: str, probe: Callable[[], T | None]) -> T:
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        found = probe()
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            raise SandboxError(f"waited {WAIT_SECONDS:.0f} seconds for {what}")
        time.sleep(POLL_SECONDS)


def reading(question: str, answer: object) -> None:
    """Something ADR 0020 asked the sandbox to settle, and what it said."""
    print(f"  read  {question}: {answer}")


@contextmanager
def sandbox_stack(
    key: str, account_id: str, webhook_secret: str | None = None
) -> Iterator[tuple[Processes, postgres.TestDatabase]]:
    """A scratch database, and the environment of an API and a worker on Increase's
    sandbox. Nothing is started here; everything is stopped and dropped on the way out."""
    database = postgres.create_database(postgres.ensure_template())
    base_url = f"http://127.0.0.1:{free_port()}"
    inherited = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("CORRIDOR_") or name.startswith("CORRIDOR_TEST_")
    }
    environment = {
        **inherited,
        "CORRIDOR_ENVIRONMENT": "test",
        "CORRIDOR_DATABASE_URL": database.app_url,
        "CORRIDOR_REDIS_URL": redis_support.redis_url(),
        "CORRIDOR_REDIS_KEY_PREFIX": redis_support.unique_prefix(),
        "CORRIDOR_JWT_SIGNING_KEY": generate_private_key_pem(),
        "CORRIDOR_API_KEY_HASH_KEY": secrets.token_urlsafe(32),
        "CORRIDOR_FX_CACHE_MAC_KEY": secrets.token_urlsafe(32),
        "CORRIDOR_BANK_RAIL_PROVIDER": "increase_sandbox",
        "CORRIDOR_INCREASE_BASE_URL": INCREASE_SANDBOX_URL,
        "CORRIDOR_INCREASE_API_KEY": key,
        "CORRIDOR_INCREASE_ACCOUNT_ID": account_id,
        "CORRIDOR_RECONCILIATION_INTERVAL_SECONDS": str(RECONCILIATION_INTERVAL_SECONDS),
        "CORRIDOR_RECONCILIATION_WINDOW_SECONDS": str(RECONCILIATION_WINDOW_SECONDS),
        "CORRIDOR_RECONCILIATION_GRACE_SECONDS": str(RECONCILIATION_GRACE_SECONDS),
    }
    if webhook_secret is not None:
        # Every event by webhook, and none by listing: the run proves the webhooks alone.
        environment["CORRIDOR_INCREASE_WEBHOOK_SECRETS"] = json.dumps([webhook_secret])
        environment["CORRIDOR_INCREASE_EVENT_POLLING"] = "false"
    processes = Processes(
        base_url=base_url,
        sim_url=NO_SIMULATOR,
        corridor_environment=environment,
        sim_environment={},
    )
    try:
        yield processes, database
    finally:
        processes.stop()
        processes.close()
        postgres.drop_database(database)


@contextmanager
def tunnel(binary: str, base_url: str) -> Iterator[str]:
    """A Cloudflare quick tunnel to the API, and its public address while it lasts."""
    process = subprocess.Popen(
        [binary, "tunnel", "--no-autoupdate", "--url", base_url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    found: list[str] = []
    announced = threading.Event()

    def read() -> None:
        # cloudflared announces the address it was given on its standard error.
        for line in process.stderr or ():
            match = TUNNEL_ADDRESS.search(line)
            if match and not found:
                found.append(match.group(0))
                announced.set()

    threading.Thread(target=read, daemon=True).start()
    try:
        if not announced.wait(TUNNEL_SECONDS):
            raise SandboxError("cloudflared did not announce a tunnel address")
        public = found[0]
        # The name takes a moment to resolve once the tunnel is announced.
        wait_for(
            "the tunnel to reach the API",
            lambda: True if _answers(f"{public}/readyz") else None,
        )
        yield public
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def _answers(url: str) -> bool:
    try:
        return httpx.get(url, timeout=10).status_code == 200
    except httpx.HTTPError:
        return False


def deposit(stack: Stack, increase: Increase, person: Person, started: datetime) -> str:
    details = stack.deposit_instruction(person)["details"]
    check(
        details["rail"] == "ach" and details["account_number"].isdigit(),
        f"{person.name} is given an Increase account number to send dollars to",
    )
    number_id = increase.account_number_id(details["account_number"], started)
    inbound = increase.call(
        "POST",
        "/simulations/inbound_ach_transfers",
        json={
            "account_number_id": number_id,
            "amount": 500_00,
            "company_name": "SANDBOX RUN",
            "company_entry_description": "DEPOSIT",
        },
    )
    reading("a simulated inbound transfer's status", inbound.get("status"))
    stack.wait_for_available(person, "USD", Decimal("500.00"))
    check(True, "Increase sends 500.00 USD by ACH, and her balance is 500.00")
    return str(inbound["id"])


def settled_withdrawal(stack: Stack, increase: Increase, person: Person, beneficiary: str) -> str:
    withdrawal = stack.withdraw(person, beneficiary, "40.00")
    transfer = wait_for(
        "the worker to send the ACH transfer", lambda: increase.transfer_for(withdrawal["id"])
    )
    check(
        transfer["amount"] == 40_00 and transfer["status"] != "returned",
        "she withdraws 40.00 USD, and Corridor sends Increase an ACH transfer for it",
    )
    settled = increase.call("POST", f"/simulations/ach_transfers/{transfer['id']}/settle")
    reading("a settled transfer's status", settled.get("status"))
    reading("its settlement is filled in", settled.get("settlement") is not None)
    ended = stack.wait_for_withdrawal(person, withdrawal["id"])
    check(
        ended["status"] == "completed", "Increase settles the transfer and the withdrawal completes"
    )
    return str(transfer["id"])


def returned_withdrawal(stack: Stack, increase: Increase, person: Person, beneficiary: str) -> str:
    before = stack.available(person)
    withdrawal = stack.withdraw(person, beneficiary, "25.00")
    transfer = wait_for(
        "the worker to send the ACH transfer", lambda: increase.transfer_for(withdrawal["id"])
    )
    if transfer["status"] == "pending_submission":
        increase.call("POST", f"/simulations/ach_transfers/{transfer['id']}/submit")
    returned = increase.call(
        "POST", f"/simulations/ach_transfers/{transfer['id']}/return", json={"reason": "no_account"}
    )
    reading("a returned transfer's status", returned.get("status"))
    ended = stack.wait_for_withdrawal(person, withdrawal["id"])
    check(
        ended["status"] == "failed" and stack.available(person) == before,
        "Increase returns the next transfer: the withdrawal fails and the funds come back",
    )
    return str(transfer["id"])


async def delivered(owner_url: str) -> int:
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            found = await connection.execute(
                text("SELECT count(*) FROM webhook_events WHERE provider = 'increase'")
            )
            return int(found.scalar_one())
    finally:
        await engine.dispose()


async def open_breaks(owner_url: str) -> list[dict[str, Any]]:
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            runs = (
                await connection.execute(
                    text("SELECT count(*) FROM recon_runs WHERE status = 'completed'")
                )
            ).scalar_one()
            if not runs:
                raise SandboxError("no reconciliation run has completed")
            found = await connection.execute(
                text(
                    "SELECT kind, provider_ref FROM recon_breaks WHERE status = 'open' ORDER BY id"
                )
            )
            return [dict(row) for row in found.mappings()]
    finally:
        await engine.dispose()


def reconciled(database: postgres.TestDatabase, ours: set[str]) -> None:
    # One run that started after the last movement, so that it saw all of them.
    after = time.monotonic() + RECONCILIATION_INTERVAL_SECONDS * 2 + 5
    while time.monotonic() < after:
        time.sleep(POLL_SECONDS)
    breaks = asyncio.run(open_breaks(database.owner_url))
    for found in breaks:
        mine = "this run" if found["provider_ref"] in ours else "earlier runs or the account"
        print(f"  note  open break {found['kind']} on {found['provider_ref']} ({mine})")
    check(
        not any(found["provider_ref"] in ours for found in breaks),
        "reconciliation finds nothing wrong with this run's deposits and payouts",
    )


def scenario(processes: Processes, database: postgres.TestDatabase, increase: Increase) -> None:
    started = datetime.now(UTC).replace(microsecond=0)
    stack = Stack(processes.base_url, NO_SIMULATOR, wait_seconds=WAIT_SECONDS)
    try:
        check(stack.api.get("/readyz").status_code == 200, "the API is up")
        ana = stack.register("Ana Lima")
        inbound_id = deposit(stack, increase, ana, started)
        beneficiary = stack.add_beneficiary(ana)
        check(True, "she saves a bank account, which Increase keeps as an external account")
        paid = settled_withdrawal(stack, increase, ana, beneficiary)
        returned = returned_withdrawal(stack, increase, ana, beneficiary)
        processes.check_alive()
    finally:
        stack.close()

    verified = subprocess.run(
        [sys.executable, "-m", "corridor", "verify-ledger"],
        cwd=ROOT,
        env=processes.corridor_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    print(verified.stdout.strip() or verified.stderr.strip())
    check(verified.returncode == 0, "corridor verify-ledger exits 0")
    reconciled(database, {inbound_id, paid, returned})
    if processes.corridor_environment.get("CORRIDOR_INCREASE_EVENT_POLLING") == "false":
        count = asyncio.run(delivered(database.owner_url))
        reading("Increase webhooks received and verified, with listing off", count)
        check(count > 0, "every event came by webhook, verified by its signature")


def run_by_webhook(
    processes: Processes,
    database: postgres.TestDatabase,
    increase: Increase,
    binary: str,
    webhook_secret: str,
) -> None:
    with tunnel(binary, processes.base_url) as public:
        print(f"  note  the API is reachable at {public} for this run")
        subscription = increase.subscribe(f"{public}/v1/webhooks/increase", webhook_secret)
        try:
            processes.start_worker()
            scenario(processes, database, increase)
        finally:
            increase.unsubscribe(subscription)


def main() -> int:
    try:
        key = required("CORRIDOR_INCREASE_API_KEY")
    except SandboxError as missing:
        print(f"Sandbox run not started: {missing}")
        return 2
    increase = Increase(key)
    try:
        # An empty variable is not an account: a workflow variable never set is one.
        try:
            account_id = os.environ.get("CORRIDOR_INCREASE_ACCOUNT_ID", "") or increase.account()
        except (SandboxError, httpx.HTTPError) as failure:
            print(f"Sandbox run not started: {failure}")
            return 1
        print(f"  note  moving money through {account_id}")
        tunnel_binary = os.environ.get("CORRIDOR_SANDBOX_TUNNEL", "")
        # Never printed: it is the key Increase signs this run's webhooks with.
        webhook_secret = secrets.token_urlsafe(36) if tunnel_binary else None
        with sandbox_stack(key, account_id, webhook_secret) as (processes, database):
            try:
                processes.start_api()
                if tunnel_binary and webhook_secret is not None:
                    run_by_webhook(processes, database, increase, tunnel_binary, webhook_secret)
                else:
                    processes.start_worker()
                    scenario(processes, database, increase)
            except (
                AssertionError,
                DemoError,
                SandboxError,
                RuntimeError,
                httpx.HTTPError,
            ) as failure:
                print(f"\nSandbox run failed: {failure}")
                processes.stop()
                processes.print_output()
                return 1
    finally:
        increase.close()
    print("Sandbox run passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
