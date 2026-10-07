"""End to end: the simulator, the API and the worker as real processes, and a scenario
driven through them over HTTP.

The test suite runs the three in one process on a clock it controls. This proves the other
half: that the installed package starts them from the command line, that they find each
other over real sockets, and that money moves between them in real time. It uses a scratch
database created the same way the tests create theirs, so it needs the same two environment
variables:

    CORRIDOR_TEST_POSTGRES_ADMIN_URL
    CORRIDOR_TEST_REDIS_URL

Run the scenario with ``uv run poe e2e``. ``uv run poe demo`` starts the same stack and
runs ``corridor demo`` against it.
"""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import IO, Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.support import postgres  # noqa: E402 - needs the project root on the path
from tests.support import redis as redis_support  # noqa: E402

from corridor.demo import DemoError, Person, Stack, expect  # noqa: E402
from corridor.identity import generate_private_key_pem  # noqa: E402

STARTUP_TIMEOUT_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 10.0
# The worker of this stack reconciles every couple of seconds, and repairs a deposit the
# bank has had for a second: a deployment's five minutes and two would make the scenario
# wait that long for the one webhook it loses on purpose.
RECONCILIATION_INTERVAL_SECONDS = 2
RECONCILIATION_GRACE_SECONDS = 1
# How long the scenario waits for anything that happens behind the API.
WAIT_SECONDS = 60.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def check(condition: bool, description: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        raise AssertionError(description)


@dataclass
class Running:
    """One of the stack's processes and the file its output goes to."""

    name: str
    process: subprocess.Popen[bytes]
    output: IO[bytes]

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def said(self) -> str:
        self.output.seek(0)
        return self.output.read().decode(errors="replace")


@dataclass
class Processes:
    """What was started, the addresses it listens on and the environment it runs in."""

    base_url: str
    sim_url: str
    corridor_environment: dict[str, str] = field(repr=False)
    sim_environment: dict[str, str] = field(repr=False)
    running: list[Running] = field(default_factory=list)

    def start(self, name: str, command: list[str], environment: dict[str, str]) -> Running:
        print(f"Starting {name}: python {' '.join(command)}")
        output = tempfile.TemporaryFile()  # noqa: SIM115 - closed when the stack stops
        process = subprocess.Popen(
            [sys.executable, *command],
            cwd=ROOT,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        started = Running(name, process, output)
        self.running.append(started)
        return started

    def start_simulator(self) -> None:
        simulator = self.start("simulator", ["-m", "corridor_sim"], self.sim_environment)
        # Anything but a refused connection means it is listening: it has no health route.
        wait_until_up(self.sim_url + "/_control/clock", simulator)

    def start_api(self) -> None:
        port = self.base_url.rsplit(":", 1)[1]
        api = self.start(
            "API",
            ["-m", "corridor", "serve", "--host", "127.0.0.1", "--port", port],
            self.corridor_environment,
        )
        wait_until_up(self.base_url + "/readyz", api)

    def start_worker(self) -> None:
        self.start("worker", ["-m", "corridor", "worker"], self.corridor_environment)

    def check_alive(self) -> None:
        for one in self.running:
            if one.process.poll() is not None:
                raise RuntimeError(f"the {one.name} exited with code {one.process.returncode}")

    def stop(self) -> None:
        # The newest first: the worker and the API before the simulator they call.
        for one in reversed(self.running):
            one.stop()

    def print_output(self) -> None:
        for one in self.running:
            print(f"--- {one.name} output ---")
            print(one.said())

    def close(self) -> None:
        for one in self.running:
            one.output.close()


def wait_until_up(url: str, running: Running) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if running.process.poll() is not None:
            raise RuntimeError(
                f"the {running.name} exited during start-up with code {running.process.returncode}"
            )
        try:
            if httpx.get(url, timeout=1.0).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise RuntimeError(
        f"the {running.name} did not answer within {STARTUP_TIMEOUT_SECONDS:.0f} seconds"
    )


@contextmanager
def scratch_stack() -> Iterator[Processes]:
    """A scratch database and what the three processes need to run against it. Nothing is
    started here. Everything that was started is stopped, and the database dropped, on the
    way out, whatever happened in between."""
    database = postgres.create_database(postgres.ensure_template())
    api_port, sim_port = free_port(), free_port()
    base_url, sim_url = f"http://127.0.0.1:{api_port}", f"http://127.0.0.1:{sim_port}"
    # Generated for this run and held in memory only.
    sim_api_key = secrets.token_urlsafe(32)
    bank_secret, custody_secret = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    # Whatever the caller's shell holds for either program is left out: this run's
    # configuration is exactly what is written here.
    inherited = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("CORRIDOR_") or name.startswith("CORRIDOR_TEST_")
    }
    processes = Processes(
        base_url=base_url,
        sim_url=sim_url,
        corridor_environment={
            **inherited,
            "CORRIDOR_ENVIRONMENT": "test",
            "CORRIDOR_DATABASE_URL": database.app_url,
            "CORRIDOR_REDIS_URL": redis_support.redis_url(),
            "CORRIDOR_REDIS_KEY_PREFIX": redis_support.unique_prefix(),
            "CORRIDOR_JWT_SIGNING_KEY": generate_private_key_pem(),
            "CORRIDOR_API_KEY_HASH_KEY": secrets.token_urlsafe(32),
            "CORRIDOR_FX_CACHE_MAC_KEY": secrets.token_urlsafe(32),
            "CORRIDOR_BANK_RAIL_URL": sim_url,
            "CORRIDOR_CUSTODY_URL": sim_url,
            "CORRIDOR_FX_RATES_URL": sim_url,
            "CORRIDOR_BANK_RAIL_API_KEY": sim_api_key,
            "CORRIDOR_CUSTODY_API_KEY": sim_api_key,
            "CORRIDOR_FX_RATES_API_KEY": sim_api_key,
            "CORRIDOR_BANK_RAIL_WEBHOOK_SECRETS": json.dumps([bank_secret]),
            "CORRIDOR_CUSTODY_WEBHOOK_SECRETS": json.dumps([custody_secret]),
            "CORRIDOR_RECONCILIATION_INTERVAL_SECONDS": str(RECONCILIATION_INTERVAL_SECONDS),
            "CORRIDOR_RECONCILIATION_GRACE_SECONDS": str(RECONCILIATION_GRACE_SECONDS),
        },
        sim_environment={
            **inherited,
            "CORRIDOR_SIM_ENVIRONMENT": "test",
            "CORRIDOR_SIM_HOST": "127.0.0.1",
            "CORRIDOR_SIM_PORT": str(sim_port),
            "CORRIDOR_SIM_API_KEY": sim_api_key,
            "CORRIDOR_SIM_BANK_WEBHOOK_URL": f"{base_url}/v1/webhooks/simbank",
            "CORRIDOR_SIM_CUSTODY_WEBHOOK_URL": f"{base_url}/v1/webhooks/simcustody",
            "CORRIDOR_SIM_BANK_WEBHOOK_SECRET": bank_secret,
            "CORRIDOR_SIM_CUSTODY_WEBHOOK_SECRET": custody_secret,
            # An ACH payout settles in half a minute by default, which is a long time to
            # watch a script wait.
            "CORRIDOR_SIM_ACH_SETTLE_SECONDS": "2",
        },
    )
    try:
        yield processes
    finally:
        processes.stop()
        processes.close()
        postgres.drop_database(database)


def available(stack: Stack, person: Person, asset: str = "USD") -> Decimal:
    return stack.available(person, asset)


def arrives(stack: Stack, person: Person, asset: str, amount: str) -> bool:
    """Whether the person's available balance becomes ``amount`` before the wait is over."""
    try:
        stack.wait_for_available(person, asset, Decimal(amount))
    except DemoError:
        return False
    return True


def lost_deposit(stack: Stack, bruno: Person) -> None:
    """A deposit whose webhook never arrives, while the worker is running: only its next
    reconciliation run can find it."""
    stack.control("POST", "/webhooks/behaviour", {"drop_types": ["deposit.received"]})
    deposit_id = stack.bank_deposit(bruno, "75.00")
    # The simulator delivers on its own clock. Give it time to have delivered, had it
    # been going to.
    time.sleep(1.0)
    events = stack.control("GET", "/webhooks/events")["events"]
    (lost,) = [event for event in events if event["data"].get("deposit_id") == deposit_id]
    check(
        lost["status"] == "dropped" and lost["attempts"] == [],
        "the bank received 75.00 USD for Bruno and its webhook was dropped, never sent",
    )
    stack.control("POST", "/webhooks/behaviour", {"drop_types": []})


def agent_payments(stack: Stack, ana: Person, bruno: Person) -> None:
    """An agent of Ana's pays Bruno: once under the amount it may send unseen, and once
    over it, which moves nothing until Ana approves."""
    created = stack.api.post("/v1/agents", json={"name": "Bill payer"}, headers=ana.headers)
    agent_id = expect(created, 201, "create an agent")["id"]
    issued = stack.api.post(
        f"/v1/agents/{agent_id}/keys", json={"scopes": ["transfers:create"]}, headers=ana.headers
    )
    agent = {"Authorization": f"Bearer {expect(issued, 201, 'issue an agent key')['key']}"}
    policy = stack.api.put(
        f"/v1/agents/{agent_id}/policy",
        json={
            "per_tx_usd": "100.00",
            "daily_usd": "200.00",
            "approval_threshold_usd": "20.00",
            "allowed_recipients": [{"kind": "user", "id": bruno.id}],
        },
        headers=ana.headers,
    )
    check(policy.status_code == 200, "Ana gives an agent a key and a policy: ask above 20.00 USD")

    ana_before, bruno_before = available(stack, ana), available(stack, bruno)
    small = stack.transfer(agent, bruno, "10.00")
    check(
        small.status_code == 201
        and available(stack, ana) == ana_before - Decimal("10.00")
        and available(stack, bruno) == bruno_before + Decimal("10.00"),
        "the agent sends Bruno 10.00 USD, under the threshold, and it moves at once",
    )

    large = stack.transfer(agent, bruno, "30.00")
    check(
        large.status_code == 202 and available(stack, ana) == ana_before - Decimal("10.00"),
        "the agent asks to send 30.00 USD, over the threshold, and nothing moves",
    )
    approval_id = large.json()["approval_request"]["id"]
    refused = stack.api.post(f"/v1/approvals/{approval_id}/approve", headers=agent)
    check(refused.status_code in (401, 403), "the agent cannot approve its own request")
    approved = stack.api.post(f"/v1/approvals/{approval_id}/approve", headers=ana.headers)
    check(
        approved.status_code == 200
        and approved.json()["status"] == "executed"
        and available(stack, ana) == ana_before - Decimal("40.00")
        and available(stack, bruno) == bruno_before + Decimal("40.00"),
        "Ana approves, and the 30.00 USD moves",
    )
    stack.api.post(f"/v1/approvals/{approval_id}/approve", headers=ana.headers)
    check(
        available(stack, ana) == ana_before - Decimal("40.00")
        and available(stack, bruno) == bruno_before + Decimal("40.00"),
        "approving a second time moves nothing more",
    )


def scenario(processes: Processes) -> None:
    stack = Stack(processes.base_url, processes.sim_url, wait_seconds=WAIT_SECONDS)
    try:
        ready = stack.api.get("/readyz")
        check(ready.status_code == 200, "the API is up and reaches PostgreSQL and Redis")

        ana, bruno = stack.register("Ana Lima"), stack.register("Bruno Costa")
        check(
            available(stack, ana) == 0 and available(stack, bruno) == 0,
            "two users register and start with nothing",
        )

        details = stack.deposit_instruction(ana)["details"]
        check(
            details["rail"] == "ach" and details["account_number"].isdigit(),
            "Ana is told where to send US dollars",
        )
        stack.bank_deposit(ana, "500.00")
        check(
            arrives(stack, ana, "USD", "500.00"),
            "the bank receives 500.00 USD for Ana, sends its webhook, and her balance is 500.00",
        )

        quote = stack.quote(ana, "USD", "MXN", "100.00")
        converted = stack.convert(ana, quote["id"])
        check(
            converted["buy_amount"] == quote["buy_amount"]
            and available(stack, ana) == Decimal("400.00")
            and available(stack, ana, "MXN") == Decimal(quote["buy_amount"]),
            f"Ana converts 100.00 USD into {quote['buy_amount']} MXN at the quoted rate",
        )

        sent = stack.transfer(ana.headers, bruno, "50.00")
        check(
            sent.status_code == 201
            and sent.json()["status"] == "completed"
            and available(stack, ana) == Decimal("350.00") - Decimal(sent.json()["fee"])
            and available(stack, bruno) == Decimal("50.00"),
            "Ana sends Bruno 50.00 USD, and he has 50.00",
        )

        lost_deposit(stack, bruno)
        check(
            arrives(stack, bruno, "USD", "125.00"),
            "reconciliation finds the deposit at the bank mid-run, and Bruno has 125.00",
        )

        beneficiary_id = stack.add_beneficiary(bruno)
        withdrawal = stack.withdraw(bruno, beneficiary_id, "40.00")
        taken = Decimal("40.00") + Decimal(withdrawal["fee"])
        check(
            available(stack, bruno) == Decimal("125.00") - taken,
            f"Bruno withdraws 40.00 USD to his bank: it and a fee of {withdrawal['fee']} are held",
        )
        settled = stack.wait_for_withdrawal(bruno, withdrawal["id"])
        wallet = stack.wallets(bruno)["USD"]
        check(
            settled["status"] == "completed"
            and Decimal(wallet["held"]) == 0
            and Decimal(wallet["available"]) == Decimal("125.00") - taken,
            "the bank's webhook settles the withdrawal and nothing is left on hold",
        )
        payouts: list[dict[str, Any]] = stack.control("GET", "/bank/payouts")["payouts"]
        check(
            [(payout["reference"], payout["amount"]) for payout in payouts]
            == [(withdrawal["id"], "40.00")],
            "the bank paid out 40.00 USD, once",
        )

        agent_payments(stack, ana, bruno)
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


def run_scenario() -> int:
    with scratch_stack() as processes:
        try:
            processes.start_simulator()
            processes.start_api()
            processes.start_worker()
            scenario(processes)
        except Exception as failure:
            print(f"\nEnd-to-end run failed: {failure}")
            processes.stop()
            processes.print_output()
            return 1
    print("End-to-end run passed.")
    return 0


def run_demo() -> int:
    with scratch_stack() as processes:
        try:
            processes.start_simulator()
            processes.start_api()
            processes.start_worker()
            print()
            demo = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "corridor",
                    "demo",
                    "--base-url",
                    processes.base_url,
                    "--sim-url",
                    processes.sim_url,
                ],
                cwd=ROOT,
                # The demo ends by verifying the ledger, which reads the database from
                # the same settings the API runs with.
                env=processes.corridor_environment,
                check=False,
            )
            processes.check_alive()
        except Exception as failure:
            print(f"\nThe demo could not be run: {failure}")
            processes.stop()
            processes.print_output()
            return 1
        if demo.returncode != 0:
            processes.stop()
            processes.print_output()
        return demo.returncode


def main(arguments: list[str]) -> int:
    if arguments == ["demo"]:
        return run_demo()
    if arguments:
        print("usage: python scripts/e2e.py [demo]")
        return 2
    return run_scenario()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
