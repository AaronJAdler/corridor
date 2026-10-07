"""Passwords: hashing, the one-verification rule, and login with its lockout."""

import asyncio
import hashlib
import threading
from datetime import timedelta

import argon2
import pytest
from sqlalchemy import text

from corridor import identity
from corridor.identity import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    LoginCandidate,
    LoginOutcome,
    PasswordHasher,
    User,
    WeakPassword,
    validate_password,
)
from corridor.platform.clock import ManualClock, utcnow
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from tests.identity.support import (
    PASSWORD,
    PASSWORD_HASH,
    WRONG_PASSWORD,
    close_account,
    user_row,
)


@pytest.fixture
def verifications(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every hash the underlying Argon2 verifier is asked to check, in order."""
    checked: list[str] = []
    real = argon2.PasswordHasher.verify

    def spy(self: argon2.PasswordHasher, hash: str | bytes, password: str | bytes) -> bool:
        checked.append(hash if isinstance(hash, str) else hash.decode())
        return real(self, hash, password)

    monkeypatch.setattr(argon2.PasswordHasher, "verify", spy)
    return checked


# --- hashing ---------------------------------------------------------------------------------


async def test_a_hash_is_argon2id_salted_and_not_the_password(hasher: PasswordHasher) -> None:
    hashed = await hasher.hash(PASSWORD)

    assert hashed.startswith("$argon2id$")
    assert PASSWORD not in hashed
    assert await hasher.hash(PASSWORD) != hashed


async def test_the_right_password_verifies_and_a_wrong_one_does_not(
    hasher: PasswordHasher,
) -> None:
    hashed = await hasher.hash(PASSWORD)

    assert await hasher.verify(hashed, PASSWORD) is True
    assert await hasher.verify(hashed, WRONG_PASSWORD) is False


@pytest.mark.parametrize(
    "case", ["unknown email", "wrong password", "right password", "malformed hash"]
)
async def test_verifying_costs_exactly_one_argon2_verification(
    hasher: PasswordHasher, verifications: list[str], case: str
) -> None:
    stored = await hasher.hash(PASSWORD)
    password_hash, password, matches = {
        "unknown email": (None, PASSWORD, False),
        "wrong password": (stored, WRONG_PASSWORD, False),
        "right password": (stored, PASSWORD, True),
        "malformed hash": ("not-a-hash", PASSWORD, False),
    }[case]

    assert await hasher.verify(password_hash, password) is matches

    assert len(verifications) == 1


async def test_an_unknown_user_is_verified_against_a_hash_with_the_same_parameters(
    hasher: PasswordHasher, verifications: list[str]
) -> None:
    real = await hasher.hash(PASSWORD)

    await hasher.verify(None, PASSWORD)

    [dummy] = verifications
    assert dummy != real
    # The same parameters cost the same time, which is the point of the dummy.
    assert argon2.extract_parameters(dummy) == argon2.extract_parameters(real)


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "not-a-hash",
        "$argon2id$garbage",
        "$2b$12$abcdefghijklmnopqrstuv",
        "$argon2id$v=19$m=8,t=1,p=1$c2FsdHNhbHQ$é",
        "$argon2id$v=19$m=8,t=1,p=1$!!!$!!!",
        PASSWORD,
    ],
)
async def test_a_stored_value_that_is_not_a_usable_hash_never_verifies_and_never_raises(
    hasher: PasswordHasher, verifications: list[str], stored: str
) -> None:
    assert await hasher.verify(stored, PASSWORD) is False
    assert len(verifications) == 1


@pytest.mark.parametrize(
    "stored", ["", "not-a-hash", "$argon2id$garbage", "$2b$12$abcdefghijklmnopqrstuv"]
)
async def test_a_stored_value_that_is_not_an_argon2_hash_still_costs_a_real_verification(
    hasher: PasswordHasher, verifications: list[str], stored: str
) -> None:
    real = await hasher.hash(PASSWORD)

    await hasher.verify(stored, PASSWORD)

    # Argon2 rejects such a value without doing any work, so the dummy is verified instead.
    [verified] = verifications
    assert argon2.extract_parameters(verified) == argon2.extract_parameters(real)


async def test_matching_the_dummy_hash_is_not_a_login(hasher: PasswordHasher) -> None:
    # The dummy's own password is random and thrown away. Plant a known one, to show that
    # even a match against the dummy verifies nobody.
    hasher._dummy_hash = await hasher.hash(PASSWORD)

    assert await hasher.verify(None, PASSWORD) is False
    assert await hasher.verify("not-a-hash", PASSWORD) is False


async def test_hashing_and_verifying_run_off_the_event_loop(
    hasher: PasswordHasher, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran_in: dict[str, int] = {}
    real_hash, real_verify = argon2.PasswordHasher.hash, argon2.PasswordHasher.verify

    def hash_spy(self: argon2.PasswordHasher, password: str | bytes) -> str:
        ran_in["hash"] = threading.get_ident()
        return real_hash(self, password)

    def verify_spy(self: argon2.PasswordHasher, hash: str | bytes, password: str | bytes) -> bool:
        ran_in["verify"] = threading.get_ident()
        return real_verify(self, hash, password)

    monkeypatch.setattr(argon2.PasswordHasher, "hash", hash_spy)
    monkeypatch.setattr(argon2.PasswordHasher, "verify", verify_spy)

    await hasher.verify(await hasher.hash(PASSWORD), PASSWORD)

    # Argon2 is tens of milliseconds of CPU. On the loop's thread it would stall every
    # other request for that long.
    assert ran_in.keys() == {"hash", "verify"}
    assert threading.get_ident() not in ran_in.values()


# --- password rules --------------------------------------------------------------------------


def test_the_length_limits_are_12_and_128() -> None:
    assert (MIN_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH) == (12, 128)


@pytest.mark.parametrize("length", [12, 13, 127, 128])
def test_a_password_of_12_to_128_characters_is_accepted(length: int) -> None:
    validate_password("x" * length)


@pytest.mark.parametrize("length", [0, 1, 11, 129, 10_000])
def test_a_password_shorter_than_12_or_longer_than_128_is_weak(length: int) -> None:
    with pytest.raises(WeakPassword) as refusal:
        validate_password("x" * length)

    assert (refusal.value.status, refusal.value.code) == (422, "weak_password")


@pytest.mark.parametrize("candidate", ["a" * 12, "1" * 12, " " * 12, "пароль-пароль", "🙂" * 12])
def test_a_password_needs_no_particular_mix_of_characters(candidate: str) -> None:
    validate_password(candidate)


# --- parameters ------------------------------------------------------------------------------


async def test_a_hash_made_with_the_current_parameters_needs_no_rehash(
    hasher: PasswordHasher,
) -> None:
    assert hasher.needs_rehash(await hasher.hash(PASSWORD)) is False


@pytest.mark.parametrize(
    "stronger",
    [{"argon2_time_cost": 2}, {"argon2_memory_cost_kib": 32}, {"argon2_parallelism": 2}],
)
async def test_a_hash_made_with_weaker_parameters_needs_a_rehash(
    settings: Settings, stronger: dict[str, int]
) -> None:
    # Memory must be at least eight times the parallelism, hence 16 rather than the fixture's 8.
    weak_settings = settings.model_copy(update={"argon2_memory_cost_kib": 16})
    weak_hash = await PasswordHasher(weak_settings).hash(PASSWORD)

    current = PasswordHasher(weak_settings.model_copy(update=stronger))

    assert current.needs_rehash(weak_hash) is True
    assert await current.verify(weak_hash, PASSWORD) is True


def test_a_value_that_is_not_a_hash_needs_a_rehash(hasher: PasswordHasher) -> None:
    assert hasher.needs_rehash("not-a-hash") is True


def test_without_overrides_the_parameters_are_the_librarys(settings: Settings) -> None:
    unset = settings.model_copy(
        update={
            "argon2_time_cost": None,
            "argon2_memory_cost_kib": None,
            "argon2_parallelism": None,
        }
    )

    hasher = PasswordHasher(unset)

    # argon2-cffi's defaults are the RFC 9106 low-memory profile.
    assert hasher.needs_rehash(argon2.PasswordHasher().hash(PASSWORD)) is False
    assert hasher.needs_rehash(PASSWORD_HASH) is True


# --- login -----------------------------------------------------------------------------------


@pytest.fixture
def strict(settings: Settings) -> Settings:
    """Lock after three failures, for a minute at first and five minutes at most."""
    return settings.model_copy(
        update={
            "login_lockout_threshold": 3,
            "login_lockout_base_seconds": 60,
            "login_lockout_max_seconds": 300,
        }
    )


@pytest.fixture
async def candidate(db: Database, maria: User) -> LoginCandidate:
    async with db.transaction() as session:
        found = await identity.find_login_candidate(session, maria.email)
    assert found is not None
    return found


# Where the attempts in these tests come from, unless a test says otherwise.
CLIENT = "203.0.113.7"
ELSEWHERE = "198.51.100.23"
EMAIL = "maria@example.com"


async def attempt(
    db: Database,
    candidate: LoginCandidate | None,
    password_ok: bool,
    settings: Settings,
    *,
    email: str = EMAIL,
    client: str = CLIENT,
) -> LoginOutcome:
    """The last step of a login in a transaction of its own, as the HTTP handler runs it."""
    return await db.run(
        lambda session: identity.complete_login(
            session, candidate, password_ok, email=email, client=client, settings=settings
        )
    )


def digest(email: str) -> str:
    """What failures to an address are counted under, worked out here from the rule."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


async def counters(db: Database, email: str = EMAIL, client: str = CLIENT) -> tuple[int, object]:
    """The failures counted against an address from one client, and until when they lock
    that client out. Nothing counted is ``(0, None)``."""
    async with db.transaction() as session:
        row = (
            await session.execute(
                text(
                    "SELECT failed_logins, locked_until FROM login_lockouts"
                    " WHERE email_hash = :hash AND client = :client"
                ),
                {"hash": digest(email), "client": client},
            )
        ).one_or_none()
    return (0, None) if row is None else (row.failed_logins, row.locked_until)


async def slowed(db: Database, email: str = EMAIL) -> int:
    """The recent failures counted against an address from everywhere."""
    async with db.transaction() as session:
        counted = (
            await session.execute(
                text("SELECT failed_logins FROM login_throttles WHERE email_hash = :hash"),
                {"hash": digest(email)},
            )
        ).scalar_one_or_none()
    return int(counted or 0)


async def delay(db: Database, settings: Settings, email: str = EMAIL) -> float:
    async with db.transaction() as session:
        return await identity.login_delay_seconds(session, email, settings=settings)


async def test_a_login_candidate_is_found_by_email_in_any_case(db: Database, maria: User) -> None:
    async with db.transaction() as session:
        for email in ("maria@example.com", "  Maria@Example.COM "):
            assert await identity.find_login_candidate(session, email) == LoginCandidate(
                user_id=maria.id, password_hash=PASSWORD_HASH
            )
        assert await identity.find_login_candidate(session, "nobody@example.com") is None
        # A handle addresses a recipient. It is not a login name.
        assert await identity.find_login_candidate(session, "maria") is None


def test_a_login_candidate_does_not_print_its_hash() -> None:
    assert PASSWORD_HASH not in repr(LoginCandidate(user_id=new_id(), password_hash=PASSWORD_HASH))


async def test_a_login_runs_in_three_steps_with_no_transaction_around_the_hashing(
    db: Database, hasher: PasswordHasher, settings: Settings, maria: User
) -> None:
    async def login(email: str, password: str) -> LoginOutcome:
        found = await db.run(lambda session: identity.find_login_candidate(session, email))
        ok = await hasher.verify(found.password_hash if found else None, password)
        return await attempt(db, found, ok, settings)

    assert await login("Maria@example.com", PASSWORD) == LoginOutcome(
        user=maria, user_id=maria.id, reason=None, locked_until=None
    )
    assert (await login("maria@example.com", WRONG_PASSWORD)).reason == "bad_password"
    assert (await login("nobody@example.com", PASSWORD)).reason == "unknown_email"


async def test_an_unknown_email_is_refused_whatever_the_password_check_said(
    db: Database, settings: Settings, maria: User
) -> None:
    for password_ok in (False, True):
        assert await attempt(
            db, None, password_ok, settings, email="nobody@example.com"
        ) == LoginOutcome(user=None, user_id=None, reason="unknown_email", locked_until=None)
    # Counted against the address that was typed, and against nobody else's.
    assert await counters(db, "nobody@example.com") == (2, None)
    assert await counters(db) == (0, None)


async def test_a_candidate_whose_account_is_gone_is_an_unknown_email(
    db: Database, settings: Settings
) -> None:
    ghost = LoginCandidate(user_id=new_id(), password_hash=PASSWORD_HASH)

    assert await attempt(db, ghost, True, settings) == LoginOutcome(
        user=None, user_id=None, reason="unknown_email", locked_until=None
    )


async def test_the_right_password_logs_in(
    db: Database, settings: Settings, maria: User, candidate: LoginCandidate
) -> None:
    assert await attempt(db, candidate, True, settings) == LoginOutcome(
        user=maria, user_id=maria.id, reason=None, locked_until=None
    )
    assert await counters(db) == (0, None)


async def test_a_wrong_password_is_counted(
    db: Database, strict: Settings, clock: ManualClock, maria: User, candidate: LoginCandidate
) -> None:
    clock.advance(minutes=5)

    first = await attempt(db, candidate, False, strict)

    assert first == LoginOutcome(
        user=None, user_id=maria.id, reason="bad_password", locked_until=None
    )
    assert await counters(db) == (1, None)

    await attempt(db, candidate, False, strict)

    assert await counters(db) == (2, None)
    async with db.transaction() as session:
        counted_at = await session.execute(text("SELECT updated_at FROM login_lockouts"))
        assert counted_at.scalar_one() == clock.now()
        # The user's own row is not what is written to: a stranger's guesses change
        # nothing about the account.
        assert (await user_row(session, maria.id))["updated_at"] == maria.created_at


async def test_reaching_the_threshold_locks_the_account(
    db: Database, strict: Settings, clock: ManualClock, maria: User, candidate: LoginCandidate
) -> None:
    for _ in range(2):
        assert (await attempt(db, candidate, False, strict)).locked_until is None

    third = await attempt(db, candidate, False, strict)

    locked_until = clock.now() + timedelta(seconds=60)
    assert third == LoginOutcome(
        user=None, user_id=maria.id, reason="bad_password", locked_until=locked_until
    )
    assert await counters(db) == (3, locked_until)


async def test_each_failure_after_the_threshold_doubles_the_lock_up_to_the_maximum(
    db: Database, strict: Settings, clock: ManualClock, maria: User, candidate: LoginCandidate
) -> None:
    for _ in range(2):
        await attempt(db, candidate, False, strict)

    # The third failure reaches the threshold. Each one after it, made once the lock before
    # has run out, locks for twice as long, until the maximum of five minutes.
    for failures, seconds in enumerate([60, 120, 240, 300, 300], start=3):
        outcome = await attempt(db, candidate, False, strict)

        locked_until = clock.now() + timedelta(seconds=seconds)
        assert (outcome.reason, outcome.locked_until) == ("bad_password", locked_until), seconds
        assert await counters(db) == (failures, locked_until)
        clock.advance(seconds=seconds)


async def test_the_right_password_during_a_lock_is_refused_and_changes_nothing(
    db: Database, strict: Settings, clock: ManualClock, maria: User, candidate: LoginCandidate
) -> None:
    for _ in range(3):
        await attempt(db, candidate, False, strict)
    locked_until = clock.now() + timedelta(seconds=60)
    clock.advance(seconds=10)

    outcome = await attempt(db, candidate, True, strict)

    assert outcome == LoginOutcome(
        user=None, user_id=maria.id, reason="locked", locked_until=locked_until
    )
    # Neither reset nor extended.
    assert await counters(db) == (3, locked_until)


async def test_a_wrong_password_during_a_lock_is_not_counted_and_does_not_extend_it(
    db: Database, strict: Settings, clock: ManualClock, maria: User, candidate: LoginCandidate
) -> None:
    for _ in range(3):
        await attempt(db, candidate, False, strict)
    locked_until = clock.now() + timedelta(seconds=60)
    clock.advance(seconds=10)

    outcome = await attempt(db, candidate, False, strict)

    assert outcome == LoginOutcome(
        user=None, user_id=maria.id, reason="locked", locked_until=locked_until
    )
    assert await counters(db) == (3, locked_until)


async def test_a_lock_runs_out_with_time(
    db: Database, strict: Settings, clock: ManualClock, maria: User, candidate: LoginCandidate
) -> None:
    for _ in range(3):
        await attempt(db, candidate, False, strict)

    clock.advance(seconds=59)
    assert (await attempt(db, candidate, True, strict)).reason == "locked"

    clock.advance(seconds=1)
    assert (await attempt(db, candidate, True, strict)).user == maria


async def test_a_successful_login_resets_the_counter_and_clears_the_lock(
    db: Database, strict: Settings, clock: ManualClock, maria: User, candidate: LoginCandidate
) -> None:
    for _ in range(3):
        await attempt(db, candidate, False, strict)
    clock.advance(seconds=60)

    assert (await attempt(db, candidate, True, strict)).user == maria

    assert await counters(db) == (0, None)
    # The count starts again from nothing: two more failures do not lock.
    for _ in range(2):
        assert (await attempt(db, candidate, False, strict)).locked_until is None
    assert await counters(db) == (2, None)


async def test_30_concurrent_wrong_passwords_are_all_counted(
    db: Database, settings: Settings, maria: User, candidate: LoginCandidate
) -> None:
    patient = settings.model_copy(update={"login_lockout_threshold": 1000})

    outcomes = await asyncio.gather(*(attempt(db, candidate, False, patient) for _ in range(30)))

    assert [outcome.reason for outcome in outcomes] == ["bad_password"] * 30
    assert await counters(db) == (30, None)


async def test_concurrent_wrong_passwords_lock_the_account_at_exactly_the_threshold(
    db: Database, strict: Settings, clock: ManualClock, maria: User, candidate: LoginCandidate
) -> None:
    outcomes = await asyncio.gather(*(attempt(db, candidate, False, strict) for _ in range(20)))

    assert (
        sorted(str(outcome.reason) for outcome in outcomes)
        == ["bad_password"] * 3 + ["locked"] * 17
    )
    assert await counters(db) == (3, clock.now() + timedelta(seconds=60))


async def test_a_closed_account_cannot_log_in(
    db: Database, settings: Settings, maria: User, candidate: LoginCandidate
) -> None:
    async with db.transaction() as session:
        await close_account(session, maria.id)
        # It is still a candidate, so its password is checked like anyone's and the time a
        # login takes does not show that the account is closed.
        assert await identity.find_login_candidate(session, maria.email) == candidate

    for password_ok in (True, False):
        assert await attempt(db, candidate, password_ok, settings) == LoginOutcome(
            user=None, user_id=maria.id, reason="closed", locked_until=None
        )
    # Counted as any failure is, so that what a closed account's address does next, lock
    # and slow down, is what every other address does.
    assert await counters(db) == (2, None)


async def test_a_restricted_account_can_still_log_in(
    db: Database, settings: Settings, maria: User, candidate: LoginCandidate
) -> None:
    async with db.transaction() as session:
        restricted = await identity.restrict_user(session, maria.id, "deposit returned")

    assert (await attempt(db, candidate, True, settings)).user == restricted


# --- the lock is one client's ----------------------------------------------------------------


async def test_failures_from_one_client_do_not_lock_another_out(
    db: Database, strict: Settings, clock: ManualClock, maria: User, candidate: LoginCandidate
) -> None:
    # A stranger who knows the address and nothing else fails until they are locked out.
    for _ in range(3):
        await attempt(db, candidate, False, strict, client=ELSEWHERE)
    assert (await attempt(db, candidate, True, strict, client=ELSEWHERE)).reason == "locked"

    # The owner, from where the owner is, logs in with the right password.
    outcome = await attempt(db, candidate, True, strict)

    assert outcome.user == maria


async def test_the_owner_logging_in_does_not_let_a_locked_out_client_back_in(
    db: Database, strict: Settings, clock: ManualClock, maria: User, candidate: LoginCandidate
) -> None:
    for _ in range(3):
        await attempt(db, candidate, False, strict, client=ELSEWHERE)
    locked_until = clock.now() + timedelta(seconds=60)

    assert (await attempt(db, candidate, True, strict)).user == maria

    assert await counters(db, client=ELSEWHERE) == (3, locked_until)
    assert (await attempt(db, candidate, True, strict, client=ELSEWHERE)).reason == "locked"


async def test_each_client_is_counted_on_its_own(
    db: Database, strict: Settings, maria: User, candidate: LoginCandidate
) -> None:
    for _ in range(2):
        await attempt(db, candidate, False, strict)
        await attempt(db, candidate, False, strict, client=ELSEWHERE)

    # Four failures in all, and neither client has reached three.
    assert await counters(db) == (2, None)
    assert await counters(db, client=ELSEWHERE) == (2, None)


async def test_each_address_is_counted_on_its_own(
    db: Database, strict: Settings, maria: User, candidate: LoginCandidate
) -> None:
    for _ in range(3):
        await attempt(db, None, False, strict, email="joao@example.com")

    assert (await attempt(db, candidate, True, strict)).user == maria


async def test_an_address_nobody_registered_locks_exactly_as_a_registered_one_does(
    db: Database, strict: Settings, clock: ManualClock, maria: User, candidate: LoginCandidate
) -> None:
    unknown = "nobody@example.com"

    real = [await attempt(db, candidate, False, strict) for _ in range(4)]
    made_up = [await attempt(db, None, False, strict, email=unknown) for _ in range(4)]

    assert [(o.reason, o.locked_until) for o in real] == [
        ("bad_password", None),
        ("bad_password", None),
        ("bad_password", clock.now() + timedelta(seconds=60)),
        ("locked", clock.now() + timedelta(seconds=60)),
    ]
    # The same counts and the same lock. Only the reason, which goes to the audit log
    # and never to the client, says there was nobody there.
    assert [o.locked_until for o in made_up] == [o.locked_until for o in real]
    assert [o.reason for o in made_up] == ["unknown_email"] * 3 + ["locked"]
    assert [o.user_id for o in made_up] == [None] * 4
    assert await counters(db, unknown) == await counters(db)
    assert await slowed(db, unknown) == await slowed(db)


async def test_an_address_is_counted_however_it_is_typed(
    db: Database, strict: Settings, maria: User, candidate: LoginCandidate
) -> None:
    for typed in ("maria@example.com", "  MARIA@Example.com ", "Maria@example.COM"):
        await attempt(db, candidate, False, strict, email=typed)

    assert (await counters(db))[0] == 3


# --- the delay is everyone's -----------------------------------------------------------------


@pytest.fixture
def throttled(settings: Settings) -> Settings:
    """Slow an address down after four failures from anywhere: by half a second, doubling
    to four seconds at most, counted over ten minutes. No client is ever locked out."""
    return settings.model_copy(
        update={
            "login_lockout_threshold": 1000,
            "login_throttle_threshold": 4,
            "login_throttle_base_seconds": 0.5,
            "login_throttle_max_seconds": 4.0,
            "login_throttle_window_seconds": 600,
        }
    )


async def fail_from_many_clients(
    db: Database, candidate: LoginCandidate | None, settings: Settings, times: int, **more: str
) -> None:
    for n in range(times):
        await attempt(db, candidate, False, settings, client=f"198.51.100.{n}", **more)


def test_the_throttle_where_nothing_is_configured() -> None:
    fields = Settings.model_fields

    # Slower to start than the lock, which is one client's: ten failures against five.
    assert fields["login_throttle_threshold"].default == 10
    assert fields["login_lockout_threshold"].default == 5
    assert fields["login_throttle_base_seconds"].default == 0.5
    assert fields["login_throttle_max_seconds"].default == 8.0
    assert fields["login_throttle_window_seconds"].default == 900


async def test_an_address_nobody_has_failed_on_is_not_slowed(
    db: Database, throttled: Settings, maria: User
) -> None:
    assert await delay(db, throttled) == 0.0


async def test_failures_from_many_clients_slow_the_address_down_from_the_threshold_on(
    db: Database, throttled: Settings, maria: User, candidate: LoginCandidate
) -> None:
    delays = []
    for n in range(9):
        delays.append(await delay(db, throttled))
        await attempt(db, candidate, False, throttled, client=f"198.51.100.{n}")

    # Nothing for the first four attempts, then half a second doubling to the maximum.
    assert delays == [0.0, 0.0, 0.0, 0.0, 0.5, 1.0, 2.0, 4.0, 4.0]
    # No single client came anywhere near being locked out.
    assert await counters(db, client="198.51.100.0") == (1, None)


async def test_the_right_password_is_delayed_and_not_refused(
    db: Database, throttled: Settings, maria: User, candidate: LoginCandidate
) -> None:
    await fail_from_many_clients(db, candidate, throttled, 6)

    held_back = await delay(db, throttled)
    outcome = await attempt(db, candidate, True, throttled)

    assert held_back == 2.0
    assert outcome == LoginOutcome(user=maria, user_id=maria.id, reason=None, locked_until=None)


async def test_the_owner_logging_in_ends_the_delay(
    db: Database, throttled: Settings, maria: User, candidate: LoginCandidate
) -> None:
    await fail_from_many_clients(db, candidate, throttled, 6)

    await attempt(db, candidate, True, throttled)

    assert await delay(db, throttled) == 0.0
    assert await slowed(db) == 0


async def test_failures_older_than_the_window_are_forgotten(
    db: Database, throttled: Settings, clock: ManualClock, maria: User, candidate: LoginCandidate
) -> None:
    await fail_from_many_clients(db, candidate, throttled, 6)

    clock.advance(seconds=599)
    still = await delay(db, throttled)
    clock.advance(seconds=1)
    after = await delay(db, throttled)
    # And the count starts again from nothing with the next failure.
    await attempt(db, candidate, False, throttled)

    assert (still, after) == (2.0, 0.0)
    assert await slowed(db) == 1


async def test_the_window_runs_from_the_last_failure(
    db: Database, throttled: Settings, clock: ManualClock, maria: User, candidate: LoginCandidate
) -> None:
    await fail_from_many_clients(db, candidate, throttled, 4)
    clock.advance(seconds=400)
    await attempt(db, candidate, False, throttled)
    clock.advance(seconds=400)

    # Eight hundred seconds after the first failure, four hundred after the last.
    assert await delay(db, throttled) == 1.0


async def test_an_address_nobody_registered_is_slowed_exactly_as_a_registered_one_is(
    db: Database, throttled: Settings, maria: User, candidate: LoginCandidate
) -> None:
    unknown = "nobody@example.com"
    await fail_from_many_clients(db, candidate, throttled, 6)
    await fail_from_many_clients(db, None, throttled, 6, email=unknown)

    # Otherwise failing a few times and timing the answer would say which addresses exist.
    assert await delay(db, throttled, unknown) == await delay(db, throttled) == 2.0


async def test_a_closed_accounts_address_is_slowed_exactly_as_any_other_is(
    db: Database, throttled: Settings, maria: User, candidate: LoginCandidate
) -> None:
    async with db.transaction() as session:
        await close_account(session, maria.id)

    for n in range(6):
        await attempt(db, candidate, True, throttled, client=f"198.51.100.{n}")

    assert await delay(db, throttled) == 2.0


async def test_an_attempt_refused_by_a_lock_is_not_counted_towards_the_delay(
    db: Database, strict: Settings, maria: User, candidate: LoginCandidate
) -> None:
    for _ in range(3):
        await attempt(db, candidate, False, strict)

    for _ in range(5):
        assert (await attempt(db, candidate, False, strict)).reason == "locked"

    assert await slowed(db) == 3


async def test_the_delay_is_bounded_however_long_the_attack(
    db: Database, throttled: Settings, maria: User, candidate: LoginCandidate
) -> None:
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO login_throttles (email_hash, failed_logins, last_failed_at)"
                " VALUES (:hash, 2000000000, :now)"
            ),
            {"hash": digest(EMAIL), "now": utcnow()},
        )

    assert await delay(db, throttled) == 4.0


async def test_30_concurrent_failures_from_30_clients_are_all_counted_towards_the_delay(
    db: Database, throttled: Settings, maria: User, candidate: LoginCandidate
) -> None:
    await asyncio.gather(
        *(attempt(db, candidate, False, throttled, client=f"198.51.100.{n}") for n in range(30))
    )

    assert await slowed(db) == 30


# --- old counts ------------------------------------------------------------------------------


async def test_counts_nothing_has_added_to_since_the_cutoff_are_purged_and_only_those(
    db: Database, strict: Settings, clock: ManualClock, maria: User, candidate: LoginCandidate
) -> None:
    await attempt(db, None, False, strict, email="old@example.com")
    clock.advance(seconds=1)
    cutoff = clock.now()
    await attempt(db, None, False, strict, email="on-the-line@example.com")
    clock.advance(seconds=1)
    await attempt(db, candidate, False, strict)

    async with db.transaction() as session:
        purged = await identity.purge_login_failures(session, older_than=cutoff)

    # One row of each table for the old address.
    assert purged == 2
    assert await counters(db, "old@example.com") == (0, None)
    assert await slowed(db, "old@example.com") == 0
    assert (await counters(db, "on-the-line@example.com"))[0] == 1
    assert await slowed(db, "on-the-line@example.com") == 1
    assert (await counters(db))[0] == 1


async def test_a_count_that_still_locks_a_client_out_is_not_purged(
    db: Database, strict: Settings, clock: ManualClock, maria: User, candidate: LoginCandidate
) -> None:
    for _ in range(3):
        await attempt(db, candidate, False, strict)
    locked_until = clock.now() + timedelta(seconds=60)

    async with db.transaction() as session:
        # A cutoff after the last failure and before the lock runs out.
        await identity.purge_login_failures(session, older_than=clock.now() + timedelta(seconds=30))

    assert await counters(db) == (3, locked_until)
