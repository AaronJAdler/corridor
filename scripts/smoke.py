"""Smoke test: start the API as a real process and probe it over a real socket.

The test suite drives the app in-process. This proves the other half: that the installed
package starts from the command line, binds a port, reaches PostgreSQL and Redis, and
answers. It uses a scratch database created the same way the tests create theirs, so it
needs the same two environment variables:

    CORRIDOR_TEST_POSTGRES_ADMIN_URL
    CORRIDOR_TEST_REDIS_URL

Run it with ``uv run poe smoke``.
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.support import postgres  # noqa: E402 - needs the project root on the path
from tests.support import redis as redis_support  # noqa: E402

STARTUP_TIMEOUT_SECONDS = 30.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def wait_until_up(base_url: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"the server exited during start-up with code {process.returncode}")
        try:
            if httpx.get(f"{base_url}/healthz", timeout=1.0).status_code == 200:
                return
        except httpx.TransportError:
            time.sleep(0.1)
    raise RuntimeError(f"the server did not answer within {STARTUP_TIMEOUT_SECONDS:.0f} seconds")


def check(condition: bool, description: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        raise AssertionError(description)


def probe(base_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=5.0) as client:
        health = client.get("/healthz")
        check(health.json() == {"status": "ok"}, "GET /healthz reports the process is up")

        ready = client.get("/readyz")
        check(
            ready.status_code == 200
            and ready.json() == {"status": "ok", "checks": {"postgres": "ok", "redis": "ok"}},
            "GET /readyz reaches PostgreSQL and Redis",
        )

        metrics = client.get("/metrics")
        check(
            metrics.status_code == 200 and "corridor_db_transaction_retries_total" in metrics.text,
            "GET /metrics serves Prometheus metrics",
        )

        missing = client.get("/v1/does-not-exist")
        body = missing.json()
        check(
            missing.status_code == 404
            and missing.headers["content-type"] == "application/problem+json"
            and body["code"] == "not_found"
            and body["request_id"] == missing.headers["x-request-id"],
            "an unknown route is a problem document that carries the request id",
        )
        check("server" not in missing.headers, "the server header is not sent")


def main() -> int:
    database = postgres.create_database(postgres.ensure_template())
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = {
        **os.environ,
        "CORRIDOR_ENVIRONMENT": "test",
        "CORRIDOR_DATABASE_URL": database.app_url,
        "CORRIDOR_REDIS_URL": redis_support.redis_url(),
        "CORRIDOR_REDIS_KEY_PREFIX": redis_support.unique_prefix(),
    }
    command = [
        sys.executable,
        "-m",
        "corridor",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]

    print(f"Starting: python -m corridor serve --port {port}")
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            command, cwd=ROOT, env=environment, stdout=output, stderr=subprocess.STDOUT
        )
        try:
            wait_until_up(base_url, process)
            probe(base_url)
        except Exception as failure:
            output.seek(0)
            print(f"\nSmoke test failed: {failure}\n--- server output ---")
            print(output.read().decode(errors="replace"))
            return 1
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            postgres.drop_database(database)

    print("Smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
