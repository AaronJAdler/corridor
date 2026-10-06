"""Passwords: hashing, the one-verification rule, and login with its lockout."""

import asyncio
import threading
from datetime import timedelta

import argon2
import pytest

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
from corridor.platform.clock import ManualClock
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


async def attempt(
    db: Database, candidate: LoginCandidate | None, password_ok: bool, settings: Settings
) -> LoginOutcome:
    """The last step of a login in a transaction of its own, as the HTTP handler runs it."""
    return await db.run(
        lambda session: identity.complete_login(session, candidate, password_ok, settings=settings)
    )


async def counters(db: Database, user: User) -> tuple[int, object]:
    async with db.transaction() as session:
        row = await user_row(session, user.id)
    return row["failed_logins"], row["locked_until"]


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
        assert await attempt(db, None, password_ok, settings) == LoginOutcome(
            user=None, user_id=None, reason="unknown_email", locked_until=None
        )
    assert await counters(db, maria) == (0, None)


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
    assert await counters(db, maria) == (0, None)


async def test_a_wrong_password_is_counted(
    db: Database, strict: Settings, clock: ManualClock, maria: User, candidate: LoginCandidate
) -> None:
    clock.advance(minutes=5)

    first = await attempt(db, candidate, False, strict)

    assert first == LoginOutcome(
        user=None, user_id=maria.id, reason="bad_password", locked_until=None
    )
    assert await counters(db, maria) == (1, None)

    await attempt(db, candidate, False, strict)

    assert await counters(db, maria) == (2, None)
    async with db.transaction() as session:
        assert (await user_row(session, maria.id))["updated_at"] == clock.now()


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
    assert await counters(db, maria) == (3, locked_until)


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
        assert await counters(db, maria) == (failures, locked_until)
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
    assert await counters(db, maria) == (3, locked_until)


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
    assert await counters(db, maria) == (3, locked_until)


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

    assert await counters(db, maria) == (0, None)
    # The count starts again from nothing: two more failures do not lock.
    for _ in range(2):
        assert (await attempt(db, candidate, False, strict)).locked_until is None
    assert await counters(db, maria) == (2, None)


async def test_30_concurrent_wrong_passwords_are_all_counted(
    db: Database, settings: Settings, maria: User, candidate: LoginCandidate
) -> None:
    patient = settings.model_copy(update={"login_lockout_threshold": 1000})

    outcomes = await asyncio.gather(*(attempt(db, candidate, False, patient) for _ in range(30)))

    assert [outcome.reason for outcome in outcomes] == ["bad_password"] * 30
    assert await counters(db, maria) == (30, None)


async def test_concurrent_wrong_passwords_lock_the_account_at_exactly_the_threshold(
    db: Database, strict: Settings, clock: ManualClock, maria: User, candidate: LoginCandidate
) -> None:
    outcomes = await asyncio.gather(*(attempt(db, candidate, False, strict) for _ in range(20)))

    assert (
        sorted(str(outcome.reason) for outcome in outcomes)
        == ["bad_password"] * 3 + ["locked"] * 17
    )
    assert await counters(db, maria) == (3, clock.now() + timedelta(seconds=60))


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
    assert await counters(db, maria) == (0, None)


async def test_a_restricted_account_can_still_log_in(
    db: Database, settings: Settings, maria: User, candidate: LoginCandidate
) -> None:
    async with db.transaction() as session:
        restricted = await identity.restrict_user(session, maria.id, "deposit returned")

    assert (await attempt(db, candidate, True, settings)).user == restricted
