"""Signing keys, access tokens, and refresh tokens with their sessions."""

import asyncio
import base64
import dataclasses
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from jwt.algorithms import ECAlgorithm
from prometheus_client import REGISTRY
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from corridor import identity
from corridor.identity import (
    AccessClaims,
    ConfigurationError,
    InvalidToken,
    KeySet,
    RefreshOutcome,
    TokenPair,
    User,
    generate_private_key_pem,
    is_session_revoked,
    key_id,
    load_keyset,
    mark_session_revoked,
    mint_access_token,
    verify_access_token,
    write_keypair,
)
from corridor.platform.clock import ManualClock, utcnow
from corridor.platform.config import Settings
from corridor.platform.db import UNIQUE_VIOLATION, Database, constraint_of, sqlstate_of
from corridor.platform.ids import new_id
from corridor.platform.redis import RedisStore, create_redis
from tests.identity.support import add_user, close_account, count

# The public half of the ES256 example key in RFC 7515, appendix A.3, and its RFC 7638
# thumbprint. Published test vectors: there is no private key here.
RFC_7515_X = "f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU"  # pragma: allowlist secret
RFC_7515_Y = "x_FEzRu9m36HLN_tue659LNpXW6pCyStikYjKIWI5a0"  # pragma: allowlist secret
RFC_7515_THUMBPRINT = "oKIywvGUpTVTyxMQ3bwIIeQUudfr_CkLMjCE19ECD-U"  # pragma: allowlist secret

# How an unencrypted PKCS#8 PEM begins. A marker, not a key.
PKCS8_HEADER = "-----BEGIN PRIVATE KEY-----\n"  # pragma: allowlist secret


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def private_pem(key: object, encryption: object | None = None) -> str:
    return key.private_bytes(  # type: ignore[attr-defined]
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        encryption or serialization.NoEncryption(),
    ).decode()


def public_pem(key: object) -> str:
    return key.public_bytes(  # type: ignore[attr-defined]
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def with_key(settings: Settings, key: ec.EllipticCurvePrivateKey, **changes: object) -> Settings:
    return settings.model_copy(update={"jwt_signing_key": SecretStr(private_pem(key)), **changes})


def leaks(message: str, pem: str) -> bool:
    """Whether any line of a PEM's body appears in a message."""
    body = [line for line in pem.splitlines() if line and not line.startswith("-----")]
    return any(line in message for line in body)


def no_chained_error(error: BaseException) -> bool:
    """Whether a traceback of this error would show it alone, with no library error that
    describes the key printed alongside."""
    return error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)


# --- keys ------------------------------------------------------------------------------------


def test_a_key_id_is_stable_and_differs_between_keys() -> None:
    pem = generate_private_key_pem()
    loaded = [
        serialization.load_pem_private_key(pem.encode(), password=None).public_key()
        for _ in range(2)
    ]

    assert key_id(loaded[0]) == key_id(loaded[1])  # type: ignore[arg-type]
    assert key_id(new_key().public_key()) != key_id(loaded[0])  # type: ignore[arg-type]
    # 256 bits, base64url, no padding.
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", key_id(loaded[0]))  # type: ignore[arg-type]


def test_a_key_id_is_the_rfc_7638_thumbprint_of_the_key() -> None:
    numbers = ec.EllipticCurvePublicNumbers(
        x=int.from_bytes(b64url_decode(RFC_7515_X)),
        y=int.from_bytes(b64url_decode(RFC_7515_Y)),
        curve=ec.SECP256R1(),
    )

    assert key_id(numbers.public_key()) == RFC_7515_THUMBPRINT


# Private scalars whose public points have an x, and a y, that starts with a zero byte.
@pytest.mark.parametrize("scalar", [379, 43])
def test_a_coordinate_that_starts_with_zero_bytes_keeps_its_full_width(scalar: int) -> None:
    public = ec.derive_private_key(scalar, ec.SECP256R1()).public_key()
    numbers = public.public_numbers()
    assert min(numbers.x, numbers.y) >> 248 == 0

    # PyJWT renders the coordinates independently of this codebase; the canonical JSON is
    # written out by hand: members in alphabetical order, no whitespace.
    theirs = ECAlgorithm.to_jwk(public, as_dict=True)
    canonical = f'{{"crv":"P-256","kty":"EC","x":"{theirs["x"]}","y":"{theirs["y"]}"}}'

    assert key_id(public) == b64url(hashlib.sha256(canonical.encode()).digest())
    [published] = KeySet(
        signing_kid=key_id(public),
        signing_key=ec.derive_private_key(scalar, ec.SECP256R1()),
        public_keys={key_id(public): public},
    ).jwks()["keys"]
    assert (published["x"], published["y"]) == (theirs["x"], theirs["y"])
    assert (len(published["x"]), len(published["y"])) == (43, 43)


@pytest.mark.parametrize("curve", [ec.SECP256K1(), ec.SECP384R1()])
def test_a_key_on_another_curve_has_no_key_id(curve: ec.EllipticCurve) -> None:
    # secp256k1 is also 256 bits wide. Without the refusal it would be named as if P-256.
    with pytest.raises(ValueError, match="P-256"):
        key_id(ec.generate_private_key(curve).public_key())


def test_a_generated_private_key_is_unencrypted_pkcs8_on_p256() -> None:
    pem = generate_private_key_pem()

    assert pem.startswith(PKCS8_HEADER)
    loaded = serialization.load_pem_private_key(pem.encode(), password=None)
    assert isinstance(loaded, ec.EllipticCurvePrivateKey)
    assert isinstance(loaded.curve, ec.SECP256R1)
    assert generate_private_key_pem() != pem


def test_a_keyset_is_loaded_from_an_inline_key(settings: Settings) -> None:
    signing = new_key()

    keys = load_keyset(with_key(settings, signing))

    assert keys.signing_kid == key_id(signing.public_key())
    assert keys.signing_key.private_numbers() == signing.private_numbers()
    assert list(keys.public_keys) == [keys.signing_kid]
    assert keys.public_keys[keys.signing_kid] == signing.public_key()


def test_a_keyset_is_loaded_from_a_key_file(settings: Settings, tmp_path: Path) -> None:
    signing = new_key()
    path = tmp_path / "signing.pem"
    path.write_text(private_pem(signing))

    keys = load_keyset(
        settings.model_copy(update={"jwt_signing_key": None, "jwt_signing_key_file": path})
    )

    assert keys.signing_kid == key_id(signing.public_key())
    assert keys.signing_key.private_numbers() == signing.private_numbers()


def test_additional_public_keys_become_verification_keys(settings: Settings) -> None:
    signing, retired = new_key(), new_key()

    keys = load_keyset(
        with_key(
            settings,
            signing,
            # The signing key's own public half listed again changes nothing.
            jwt_additional_public_keys=[
                public_pem(retired.public_key()),
                public_pem(signing.public_key()),
            ],
        )
    )

    assert list(keys.public_keys) == [keys.signing_kid, key_id(retired.public_key())]
    assert keys.public_keys[key_id(retired.public_key())] == retired.public_key()


def test_a_keyset_cannot_be_changed_once_loaded(keys: KeySet) -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        keys.signing_kid = "another"  # type: ignore[misc]
    with pytest.raises(TypeError):
        keys.public_keys["another"] = new_key().public_key()  # type: ignore[index]
    assert "PrivateKey" not in repr(keys)


def test_the_jwks_publishes_public_parameters_only(settings: Settings) -> None:
    signing, retired = new_key(), new_key()
    keys = load_keyset(
        with_key(settings, signing, jwt_additional_public_keys=[public_pem(retired.public_key())])
    )

    jwks = keys.jwks()

    assert list(jwks) == ["keys"]
    assert jwks["keys"] == [
        {
            **ECAlgorithm.to_jwk(key.public_key(), as_dict=True),
            "kid": key_id(key.public_key()),
            "use": "sig",
            "alg": "ES256",
        }
        for key in (signing, retired)
    ]
    for published in jwks["keys"]:
        assert published.keys() == {"kty", "crv", "x", "y", "kid", "use", "alg"}
        assert "d" not in published
    # The private scalar, in the form a JWK would carry it, is nowhere in the document.
    private_scalar = ECAlgorithm.to_jwk(signing, as_dict=True)["d"]
    assert private_scalar not in json.dumps(jwks)


@pytest.mark.parametrize("given", ["neither", "both"])
def test_exactly_one_source_for_the_signing_key_must_be_set(
    settings: Settings, tmp_path: Path, given: str
) -> None:
    pem = private_pem(new_key())
    path = tmp_path / "signing.pem"
    path.write_text(pem)
    configured = settings.model_copy(
        update={"jwt_signing_key": SecretStr(pem), "jwt_signing_key_file": path}
        if given == "both"
        else {"jwt_signing_key": None, "jwt_signing_key_file": None}
    )

    with pytest.raises(ConfigurationError) as failure:
        load_keyset(configured)

    assert isinstance(failure.value, RuntimeError)
    message = str(failure.value)
    assert "CORRIDOR_JWT_SIGNING_KEY " in message
    assert "CORRIDOR_JWT_SIGNING_KEY_FILE" in message
    assert not leaks(message, pem)


def unusable_signing_keys() -> dict[str, str]:
    p256 = new_key()
    return {
        "a P-384 key": private_pem(ec.generate_private_key(ec.SECP384R1())),
        "a secp256k1 key": private_pem(ec.generate_private_key(ec.SECP256K1())),
        "an Ed25519 key": private_pem(ed25519.Ed25519PrivateKey.generate()),
        "an encrypted key": private_pem(
            p256, serialization.BestAvailableEncryption(b"not-a-real-passphrase")
        ),
        "a public key": public_pem(p256.public_key()),
        "half a key": private_pem(p256)[:120],
        "no key at all": "not a key",
    }


@pytest.mark.parametrize("kind", sorted(unusable_signing_keys()))
def test_a_signing_key_that_is_not_a_p256_private_key_is_refused_without_echoing_it(
    settings: Settings, tmp_path: Path, kind: str
) -> None:
    pem = unusable_signing_keys()[kind]
    path = tmp_path / "signing.pem"
    path.write_text(pem)

    for configured in (
        settings.model_copy(update={"jwt_signing_key": SecretStr(pem)}),
        settings.model_copy(update={"jwt_signing_key": None, "jwt_signing_key_file": path}),
    ):
        with pytest.raises(ConfigurationError) as failure:
            load_keyset(configured)

        assert not leaks(str(failure.value), pem)
        assert no_chained_error(failure.value)


def test_a_signing_key_file_that_cannot_be_read_is_a_configuration_error(
    settings: Settings, tmp_path: Path
) -> None:
    missing = tmp_path / "nowhere" / "signing.pem"

    with pytest.raises(ConfigurationError, match="CORRIDOR_JWT_SIGNING_KEY_FILE"):
        load_keyset(
            settings.model_copy(update={"jwt_signing_key": None, "jwt_signing_key_file": missing})
        )


def unusable_public_keys() -> dict[str, str]:
    return {
        "a P-384 key": public_pem(ec.generate_private_key(ec.SECP384R1()).public_key()),
        "an Ed25519 key": public_pem(ed25519.Ed25519PrivateKey.generate().public_key()),
        "a private key": private_pem(new_key()),
        "no key at all": "not a key",
    }


@pytest.mark.parametrize("kind", sorted(unusable_public_keys()))
def test_an_additional_key_that_is_not_a_p256_public_key_is_refused_without_echoing_it(
    settings: Settings, kind: str
) -> None:
    pem = unusable_public_keys()[kind]
    retired = public_pem(new_key().public_key())

    with pytest.raises(ConfigurationError, match="CORRIDOR_JWT_ADDITIONAL_PUBLIC_KEYS") as failure:
        load_keyset(with_key(settings, new_key(), jwt_additional_public_keys=[retired, pem]))

    assert not leaks(str(failure.value), pem)
    assert no_chained_error(failure.value)


def test_a_written_keypair_round_trips_through_the_key_file_setting(
    settings: Settings, tmp_path: Path
) -> None:
    directory = tmp_path / "keys" / "dev"

    kid, private_path = write_keypair(directory)

    assert private_path == directory / f"{kid}.pem"
    assert sorted(path.name for path in directory.iterdir()) == [f"{kid}.pem", f"{kid}.pub.pem"]
    assert private_path.read_text().startswith(PKCS8_HEADER)
    if os.name == "posix":
        assert stat.S_IMODE(private_path.stat().st_mode) == 0o600

    keys = load_keyset(
        settings.model_copy(update={"jwt_signing_key": None, "jwt_signing_key_file": private_path})
    )
    assert keys.signing_kid == kid

    # The public file is what a later configuration lists once this key is retired.
    public_file = (directory / f"{kid}.pub.pem").read_text()
    successor = load_keyset(with_key(settings, new_key(), jwt_additional_public_keys=[public_file]))
    assert kid in successor.public_keys
    assert successor.public_keys[kid] == keys.public_keys[kid]


def test_writing_a_keypair_twice_gives_two_different_keys(tmp_path: Path) -> None:
    first, _ = write_keypair(tmp_path)
    second, _ = write_keypair(tmp_path)

    assert first != second
    assert len(list(tmp_path.iterdir())) == 4


# --- access tokens ---------------------------------------------------------------------------

USER_ID = new_id()
SESSION_ID = new_id()
REFUSAL = "The access token is not valid."


def mint(keys: KeySet, settings: Settings, role: str = "user") -> str:
    token, _ = mint_access_token(
        user_id=USER_ID,
        session_id=SESSION_ID,
        role=role,  # type: ignore[arg-type]
        keys=keys,
        settings=settings,
    )
    return token


def claims_for(settings: Settings, **changes: object) -> dict[str, object]:
    """The claims of a valid token issued now, with some changed, or dropped if set to ...."""
    now = int(utcnow().timestamp())
    claims: dict[str, object] = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(USER_ID),
        "sid": str(SESSION_ID),
        "role": "user",
        "scope": "*",
        "iat": now,
        "exp": now + settings.access_token_ttl_seconds,
        "jti": str(new_id()),
    }
    claims.update(changes)
    return {name: value for name, value in claims.items() if value is not ...}


def segment(value: object) -> str:
    return b64url(json.dumps(value, separators=(",", ":")).encode())


def forge(header: dict[str, object], claims: object, key: ec.EllipticCurvePrivateKey) -> str:
    """Sign any header and payload with ES256, including ones no library agrees to produce."""
    signing_input = f"{segment(header)}.{segment(claims)}"
    signature = ECAlgorithm(ECAlgorithm.SHA256).sign(signing_input.encode(), key)
    return f"{signing_input}.{b64url(signature)}"


def signed(keys: KeySet, claims: object) -> str:
    """Claims signed properly, by the configured key and under its kid."""
    return forge({"alg": "ES256", "typ": "JWT", "kid": keys.signing_kid}, claims, keys.signing_key)


def test_a_minted_token_verifies_and_round_trips_its_claims(
    keys: KeySet, auth_settings: Settings, clock: ManualClock
) -> None:
    token, expires_at = mint_access_token(
        user_id=USER_ID, session_id=SESSION_ID, role="admin", keys=keys, settings=auth_settings
    )

    claims = verify_access_token(token, keys=keys, settings=auth_settings)

    assert claims == AccessClaims(
        user_id=USER_ID,
        session_id=SESSION_ID,
        role="admin",
        scope="*",
        issued_at=clock.now(),
        expires_at=clock.now() + timedelta(seconds=auth_settings.access_token_ttl_seconds),
        token_id=claims.token_id,
    )
    assert expires_at == claims.expires_at
    assert uuid.UUID(claims.token_id).version == 7


def test_a_minted_token_carries_exactly_the_documented_header_and_claims(
    keys: KeySet, auth_settings: Settings, clock: ManualClock
) -> None:
    header, payload, _ = mint(keys, auth_settings).split(".")

    assert json.loads(b64url_decode(header)) == {
        "alg": "ES256",
        "typ": "JWT",
        "kid": keys.signing_kid,
    }
    claims = json.loads(b64url_decode(payload))
    issued_at = int(clock.now().timestamp())
    assert claims == {
        "iss": auth_settings.jwt_issuer,
        "aud": auth_settings.jwt_audience,
        "sub": str(USER_ID),
        "sid": str(SESSION_ID),
        "role": "user",
        "scope": "*",
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": claims["jti"],
    }


def test_every_token_has_an_id_of_its_own(keys: KeySet, auth_settings: Settings) -> None:
    first = verify_access_token(mint(keys, auth_settings), keys=keys, settings=auth_settings)
    second = verify_access_token(mint(keys, auth_settings), keys=keys, settings=auth_settings)

    assert first.token_id != second.token_id


def test_a_token_lives_for_the_configured_time_and_not_a_second_longer(
    keys: KeySet, auth_settings: Settings, clock: ManualClock
) -> None:
    brief = auth_settings.model_copy(update={"access_token_ttl_seconds": 60})
    token, expires_at = mint_access_token(
        user_id=USER_ID, session_id=SESSION_ID, role="user", keys=keys, settings=brief
    )
    assert expires_at == clock.now() + timedelta(seconds=60)

    clock.advance(seconds=59)
    assert verify_access_token(token, keys=keys, settings=brief).expires_at == expires_at

    clock.advance(seconds=1)
    with pytest.raises(InvalidToken):
        verify_access_token(token, keys=keys, settings=brief)


def test_token_times_are_whole_seconds_never_rounded_into_the_future(
    keys: KeySet, auth_settings: Settings, clock: ManualClock
) -> None:
    whole = clock.now()
    clock.advance(seconds=0.75)

    claims = verify_access_token(mint(keys, auth_settings), keys=keys, settings=auth_settings)

    assert claims.issued_at == whole
    assert claims.expires_at == whole + timedelta(seconds=auth_settings.access_token_ttl_seconds)


def test_a_token_issued_by_a_clock_up_to_30_seconds_ahead_is_accepted(
    keys: KeySet, auth_settings: Settings, clock: ManualClock
) -> None:
    ahead = int(clock.now().timestamp()) + 30

    claims = verify_access_token(
        signed(keys, claims_for(auth_settings, iat=ahead)), keys=keys, settings=auth_settings
    )

    assert claims.issued_at == clock.now() + timedelta(seconds=30)


def test_a_token_from_a_retired_key_verifies_only_while_its_public_key_is_configured(
    settings: Settings, clock: ManualClock
) -> None:
    retired, current = new_key(), new_key()
    before_rotation = with_key(settings, retired)
    token = mint(load_keyset(before_rotation), before_rotation)

    # The key is rotated: a new one signs, and the old one is kept for verification.
    rotated = with_key(
        settings, current, jwt_additional_public_keys=[public_pem(retired.public_key())]
    )
    claims = verify_access_token(token, keys=load_keyset(rotated), settings=rotated)
    assert claims.user_id == USER_ID

    # Once its tokens have had time to expire, the old key is taken out.
    removed = with_key(settings, current)
    with pytest.raises(InvalidToken):
        verify_access_token(token, keys=load_keyset(removed), settings=removed)


def test_a_token_verifies_against_the_published_jwks_with_another_verifier(
    keys: KeySet, auth_settings: Settings
) -> None:
    token = mint(keys, auth_settings)

    # What a service split out later would do: fetch the JWK Set, pick the key by kid.
    published = jwt.PyJWKSet.from_dict(keys.jwks())
    claims = jwt.decode(
        token,
        published[jwt.get_unverified_header(token)["kid"]],
        algorithms=["ES256"],
        audience=auth_settings.jwt_audience,
        issuer=auth_settings.jwt_issuer,
        options={"verify_exp": False, "verify_iat": False},
    )

    assert claims["sub"] == str(USER_ID)


def expired(keys: KeySet, settings: Settings, clock: ManualClock) -> str:
    token = mint(keys, settings)
    clock.advance(seconds=settings.access_token_ttl_seconds)
    return token


def payload_changed_after_signing(keys: KeySet, settings: Settings, clock: ManualClock) -> str:
    header, _, signature = mint(keys, settings).split(".")
    return f"{header}.{segment(claims_for(settings, role='admin'))}.{signature}"


def hs256_keyed_with_the_public_key(keys: KeySet, settings: Settings, clock: ManualClock) -> str:
    # The classic confusion: a verifier that let the token choose the algorithm would check
    # this HMAC with the "key" it holds, which is public.
    header = {"alg": "HS256", "typ": "JWT", "kid": keys.signing_kid}
    signing_input = f"{segment(header)}.{segment(claims_for(settings))}"
    secret = public_pem(keys.public_keys[keys.signing_kid]).encode()
    digest = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{b64url(digest)}"


def carrying_its_own_key(keys: KeySet, settings: Settings, clock: ManualClock) -> str:
    attacker = new_key()
    kid = key_id(attacker.public_key())
    header = {
        "alg": "ES256",
        "typ": "JWT",
        "kid": kid,
        "jwk": {**ECAlgorithm.to_jwk(attacker.public_key(), as_dict=True), "kid": kid},
    }
    return forge(header, claims_for(settings), attacker)


def es384(keys: KeySet, settings: Settings, clock: ManualClock) -> str:
    return jwt.encode(
        claims_for(settings),
        ec.generate_private_key(ec.SECP384R1()),
        algorithm="ES384",
        headers={"kid": keys.signing_kid},
    )


Forgery = Callable[[KeySet, Settings, ManualClock], str]

REFUSED: dict[str, Forgery] = {
    "expired": expired,
    "from another issuer": lambda keys, settings, clock: mint(
        keys, settings.model_copy(update={"jwt_issuer": "someone-else"})
    ),
    "for another audience": lambda keys, settings, clock: mint(
        keys, settings.model_copy(update={"jwt_audience": "another-api"})
    ),
    "for several audiences": lambda keys, settings, clock: signed(
        keys, claims_for(settings, aud=[settings.jwt_audience, "another-api"])
    ),
    "signed by another key under the same kid": lambda keys, settings, clock: forge(
        {"alg": "ES256", "typ": "JWT", "kid": keys.signing_kid}, claims_for(settings), new_key()
    ),
    "payload changed after signing": payload_changed_after_signing,
    "signed by an unknown key under its own kid": lambda keys, settings, clock: mint(
        load_keyset(with_key(settings, new_key())), settings
    ),
    "signed by the right key under an unknown kid": lambda keys, settings, clock: forge(
        {"alg": "ES256", "typ": "JWT", "kid": "retired-long-ago"},
        claims_for(settings),
        keys.signing_key,
    ),
    "without a kid": lambda keys, settings, clock: forge(
        {"alg": "ES256", "typ": "JWT"}, claims_for(settings), keys.signing_key
    ),
    "with a kid that is not a string": lambda keys, settings, clock: forge(
        {"alg": "ES256", "typ": "JWT", "kid": 7}, claims_for(settings), keys.signing_key
    ),
    "carrying its own key in the header": carrying_its_own_key,
    "alg none": lambda keys, settings, clock: (
        f"{segment({'alg': 'none', 'typ': 'JWT', 'kid': keys.signing_kid})}"
        f".{segment(claims_for(settings))}."
    ),
    "alg none, signed anyway": lambda keys, settings, clock: forge(
        {"alg": "none", "typ": "JWT", "kid": keys.signing_kid},
        claims_for(settings),
        keys.signing_key,
    ),
    "without an alg": lambda keys, settings, clock: forge(
        {"typ": "JWT", "kid": keys.signing_kid}, claims_for(settings), keys.signing_key
    ),
    "HS256 keyed with the public key": hs256_keyed_with_the_public_key,
    "ES384": es384,
    "sub that is not a UUID": lambda keys, settings, clock: signed(
        keys, claims_for(settings, sub="maria")
    ),
    "sub that is not a string": lambda keys, settings, clock: signed(
        keys, claims_for(settings, sub=7)
    ),
    "sid that is not a UUID": lambda keys, settings, clock: signed(
        keys, claims_for(settings, sid="session-one")
    ),
    "sid that is not a string": lambda keys, settings, clock: signed(
        keys, claims_for(settings, sid=7)
    ),
    "role nobody has": lambda keys, settings, clock: signed(
        keys, claims_for(settings, role="root")
    ),
    "role that is not a string": lambda keys, settings, clock: signed(
        keys, claims_for(settings, role=["admin"])
    ),
    "scope that is not a string": lambda keys, settings, clock: signed(
        keys, claims_for(settings, scope=["*"])
    ),
    "jti that is not a string": lambda keys, settings, clock: signed(
        keys, claims_for(settings, jti=7)
    ),
    "exp that is not a number": lambda keys, settings, clock: signed(
        keys, claims_for(settings, exp="tomorrow")
    ),
    "exp with a fraction": lambda keys, settings, clock: signed(
        keys, claims_for(settings, exp=utcnow().timestamp() + 900.5)
    ),
    "exp that is true": lambda keys, settings, clock: signed(keys, claims_for(settings, exp=True)),
    "exp beyond the calendar": lambda keys, settings, clock: signed(
        keys, claims_for(settings, exp=10**20)
    ),
    "iat that is not a number": lambda keys, settings, clock: signed(
        keys, claims_for(settings, iat="just now")
    ),
    "iat with a fraction": lambda keys, settings, clock: signed(
        keys, claims_for(settings, iat=utcnow().timestamp() - 0.5)
    ),
    "issued 31 seconds in the future": lambda keys, settings, clock: signed(
        keys, claims_for(settings, iat=int(utcnow().timestamp()) + 31)
    ),
    "claims that are not an object": lambda keys, settings, clock: signed(keys, ["sub", "sid"]),
    "one character added": lambda keys, settings, clock: mint(keys, settings) + "A",
    "signature cut short": lambda keys, settings, clock: mint(keys, settings)[:-4],
    "signature removed": lambda keys, settings, clock: mint(keys, settings).rsplit(".", 1)[0] + ".",
    "a space in front": lambda keys, settings, clock: " " + mint(keys, settings),
    "empty": lambda keys, settings, clock: "",
    "one word": lambda keys, settings, clock: "garbage",
    "three words": lambda keys, settings, clock: "not.a.token",
    "only dots": lambda keys, settings, clock: "....",
    "not ASCII": lambda keys, settings, clock: "é.é.é",
    "not even text": lambda keys, settings, clock: "\ud800.\ud800.\ud800",
} | {
    f"without {claim}": lambda keys, settings, clock, claim=claim: signed(  # type: ignore[misc]
        keys, claims_for(settings, **{claim: ...})
    )
    for claim in ("iss", "aud", "sub", "sid", "role", "scope", "iat", "exp", "jti")
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_a_token_that_fails_any_check_is_refused_in_the_same_words(
    keys: KeySet, auth_settings: Settings, clock: ManualClock, case: str
) -> None:
    token = REFUSED[case](keys, auth_settings, clock)

    with pytest.raises(InvalidToken) as refusal:
        verify_access_token(token, keys=keys, settings=auth_settings)

    # One message for every failure: which check failed is of use only to someone probing.
    assert str(refusal.value) == REFUSAL
    assert (refusal.value.status, refusal.value.code, refusal.value.detail) == (
        401,
        "invalid_token",
        REFUSAL,
    )
    assert refusal.value.extra == {}
    assert no_chained_error(refusal.value)


def test_the_claims_the_forgeries_start_from_are_valid(
    keys: KeySet, auth_settings: Settings, clock: ManualClock
) -> None:
    # A control for the test above: unchanged, the same claims signed the same way pass,
    # so each refusal there is down to the one thing that case changed.
    claims = verify_access_token(
        signed(keys, claims_for(auth_settings)), keys=keys, settings=auth_settings
    )

    assert (claims.user_id, claims.session_id) == (USER_ID, SESSION_ID)
    assert isinstance(claims.expires_at, datetime)


# --- sessions and refresh tokens -------------------------------------------------------------

FOREIGN_KEY_VIOLATION = "23503"


def sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def token_rows(db: Database, session_id: uuid.UUID) -> list[dict[str, object]]:
    """Every token of a session as stored, oldest first."""
    async with db.transaction() as session:
        rows = await session.execute(
            text("SELECT * FROM refresh_tokens WHERE family_id = :family ORDER BY id"),
            {"family": session_id},
        )
        return [dict(row._mapping) for row in rows]


async def start_session(db: Database, user: User, keys: KeySet, settings: Settings) -> TokenPair:
    return await db.run(
        lambda session: identity.issue_session(session, user, keys=keys, settings=settings)
    )


async def rotate(db: Database, token: str, keys: KeySet, settings: Settings) -> RefreshOutcome:
    """A refresh in a transaction of its own, as the HTTP handler runs it."""
    return await db.run(
        lambda session: identity.rotate_refresh_token(session, token, keys=keys, settings=settings)
    )


async def rotated(db: Database, token: str, keys: KeySet, settings: Settings) -> TokenPair:
    outcome = await rotate(db, token, keys, settings)
    assert outcome.tokens is not None, outcome.reason
    return outcome.tokens


async def until_a_transaction_is_waiting_for_a_lock(db: Database) -> None:
    for _ in range(1000):
        async with db.transaction() as session:
            waiting = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity"
                        " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("no transaction ever waited for a lock")


async def test_a_session_stores_a_hash_of_its_refresh_token_and_never_the_token(
    db: Database, keys: KeySet, auth_settings: Settings, clock: ManualClock, maria: User
) -> None:
    pair = await start_session(db, maria, keys, auth_settings)

    [stored] = await token_rows(db, pair.session_id)
    assert stored == {
        "id": stored["id"],
        "user_id": maria.id,
        "family_id": pair.session_id,
        "token_hash": sha256_hex(pair.refresh_token),
        "issued_at": clock.now(),
        "expires_at": clock.now() + timedelta(seconds=auth_settings.refresh_token_ttl_seconds),
        "used_at": None,
        "revoked_at": None,
    }
    assert pair.refresh_token not in {str(value) for value in stored.values()}
    # 32 random bytes, which is 43 characters of base64url.
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", pair.refresh_token)


async def test_a_session_comes_with_an_access_token_for_it(
    db: Database, keys: KeySet, auth_settings: Settings, clock: ManualClock, maria: User
) -> None:
    pair = await start_session(db, maria, keys, auth_settings)

    claims = verify_access_token(pair.access_token, keys=keys, settings=auth_settings)
    assert (claims.user_id, claims.session_id, claims.role, claims.scope) == (
        maria.id,
        pair.session_id,
        "user",
        "*",
    )
    assert pair.expires_in == auth_settings.access_token_ttl_seconds
    assert claims.expires_at == clock.now() + timedelta(seconds=pair.expires_in)


async def test_each_login_is_a_session_of_its_own(
    db: Database, keys: KeySet, auth_settings: Settings, maria: User
) -> None:
    phone = await start_session(db, maria, keys, auth_settings)
    laptop = await start_session(db, maria, keys, auth_settings)

    assert phone.session_id != laptop.session_id
    assert phone.refresh_token != laptop.refresh_token
    async with db.transaction() as session:
        assert await count(session, "refresh_tokens") == 2


def test_a_token_pair_does_not_print_its_tokens() -> None:
    pair = TokenPair(
        access_token="an-access-token",
        refresh_token="a-refresh-token",
        expires_in=900,
        session_id=SESSION_ID,
    )
    outcome = RefreshOutcome(tokens=pair, user_id=USER_ID, session_id=SESSION_ID, reason=None)

    for printed in (repr(pair), str(pair), repr(outcome)):
        assert "an-access-token" not in printed
        assert "a-refresh-token" not in printed


async def test_rotation_returns_a_new_pair_in_the_same_session_and_marks_the_old_token_used(
    db: Database, keys: KeySet, auth_settings: Settings, clock: ManualClock, maria: User
) -> None:
    first = await start_session(db, maria, keys, auth_settings)
    issued = clock.now()
    clock.advance(minutes=10)

    outcome = await rotate(db, first.refresh_token, keys, auth_settings)

    assert outcome.tokens is not None
    assert outcome == RefreshOutcome(
        tokens=outcome.tokens, user_id=maria.id, session_id=first.session_id, reason=None
    )
    second = outcome.tokens
    assert second.session_id == first.session_id
    assert second.refresh_token != first.refresh_token
    assert second.expires_in == auth_settings.access_token_ttl_seconds
    claims = verify_access_token(second.access_token, keys=keys, settings=auth_settings)
    assert (claims.user_id, claims.session_id, claims.issued_at) == (
        maria.id,
        first.session_id,
        clock.now(),
    )

    lifetime = timedelta(seconds=auth_settings.refresh_token_ttl_seconds)
    old, new = await token_rows(db, first.session_id)
    assert (old["token_hash"], old["used_at"], old["revoked_at"], old["expires_at"]) == (
        sha256_hex(first.refresh_token),
        clock.now(),
        None,
        issued + lifetime,
    )
    assert new == {
        "id": new["id"],
        "user_id": maria.id,
        "family_id": first.session_id,
        "token_hash": sha256_hex(second.refresh_token),
        "issued_at": clock.now(),
        "expires_at": clock.now() + lifetime,
        "used_at": None,
        "revoked_at": None,
    }


async def test_a_session_is_rotated_again_and_again(
    db: Database, keys: KeySet, auth_settings: Settings, maria: User
) -> None:
    pair = first = await start_session(db, maria, keys, auth_settings)

    for _ in range(5):
        pair = await rotated(db, pair.refresh_token, keys, auth_settings)

    assert pair.session_id == first.session_id
    stored = await token_rows(db, first.session_id)
    assert [row["used_at"] is not None for row in stored] == [True] * 5 + [False]
    assert len({row["token_hash"] for row in stored}) == 6


async def test_a_rotated_access_token_carries_the_role_the_user_has_now(
    db: Database, keys: KeySet, auth_settings: Settings, maria: User
) -> None:
    first = await start_session(db, maria, keys, auth_settings)
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE users SET role = 'admin' WHERE id = :id"), {"id": maria.id}
        )

    second = await rotated(db, first.refresh_token, keys, auth_settings)

    claims = verify_access_token(second.access_token, keys=keys, settings=auth_settings)
    assert claims.role == "admin"


async def test_reusing_a_rotated_token_revokes_the_family_and_the_newest_token_fails_too(
    db: Database, keys: KeySet, auth_settings: Settings, clock: ManualClock, maria: User
) -> None:
    first = await start_session(db, maria, keys, auth_settings)
    second = await rotated(db, first.refresh_token, keys, auth_settings)
    third = await rotated(db, second.refresh_token, keys, auth_settings)
    clock.advance(minutes=1)

    replay = await rotate(db, first.refresh_token, keys, auth_settings)

    assert replay == RefreshOutcome(
        tokens=None, user_id=maria.id, session_id=first.session_id, reason="reuse_detected"
    )
    stored = await token_rows(db, first.session_id)
    assert [row["revoked_at"] for row in stored] == [clock.now()] * 3
    # Nobody can tell which holder was the thief, so the session is over for both: the
    # newest token, which was never used, is refused as well.
    for token in (third.refresh_token, second.refresh_token, first.refresh_token):
        assert await rotate(db, token, keys, auth_settings) == RefreshOutcome(
            tokens=None, user_id=maria.id, session_id=first.session_id, reason="revoked"
        )
    assert len(await token_rows(db, first.session_id)) == 3


async def test_reuse_in_one_session_leaves_the_users_other_sessions_alone(
    db: Database, keys: KeySet, auth_settings: Settings, maria: User
) -> None:
    phone = await start_session(db, maria, keys, auth_settings)
    laptop = await start_session(db, maria, keys, auth_settings)
    await rotated(db, phone.refresh_token, keys, auth_settings)

    assert (await rotate(db, phone.refresh_token, keys, auth_settings)).reason == "reuse_detected"

    assert (await rotate(db, laptop.refresh_token, keys, auth_settings)).reason is None


async def test_two_concurrent_rotations_of_one_token_give_one_success_and_a_revoked_family(
    db: Database, keys: KeySet, auth_settings: Settings, clock: ManualClock, maria: User
) -> None:
    first = await start_session(db, maria, keys, auth_settings)

    outcomes = await asyncio.gather(
        rotate(db, first.refresh_token, keys, auth_settings),
        rotate(db, first.refresh_token, keys, auth_settings),
    )

    assert sorted(str(outcome.reason) for outcome in outcomes) == ["None", "reuse_detected"]
    stored = await token_rows(db, first.session_id)
    assert [row["revoked_at"] for row in stored] == [clock.now()] * 2
    [winner] = [outcome.tokens for outcome in outcomes if outcome.tokens is not None]
    assert (await rotate(db, winner.refresh_token, keys, auth_settings)).reason == "revoked"


async def test_ten_concurrent_rotations_of_one_token_give_exactly_one_success(
    db: Database, keys: KeySet, auth_settings: Settings, clock: ManualClock, maria: User
) -> None:
    first = await start_session(db, maria, keys, auth_settings)

    outcomes = await asyncio.gather(
        *(rotate(db, first.refresh_token, keys, auth_settings) for _ in range(10))
    )

    # The first to get the row rotates it. The second finds it used and ends the session.
    # The rest find the session already ended.
    assert sorted(str(outcome.reason) for outcome in outcomes) == [
        "None",
        "reuse_detected",
        *["revoked"] * 8,
    ]
    stored = await token_rows(db, first.session_id)
    assert [row["revoked_at"] for row in stored] == [clock.now()] * 2


async def test_a_refresh_token_expires(
    db: Database, keys: KeySet, auth_settings: Settings, clock: ManualClock, maria: User
) -> None:
    in_time = await start_session(db, maria, keys, auth_settings)
    too_late = await start_session(db, maria, keys, auth_settings)

    clock.advance(seconds=auth_settings.refresh_token_ttl_seconds - 1)
    assert (await rotate(db, in_time.refresh_token, keys, auth_settings)).reason is None

    clock.advance(seconds=1)
    assert await rotate(db, too_late.refresh_token, keys, auth_settings) == RefreshOutcome(
        tokens=None, user_id=maria.id, session_id=too_late.session_id, reason="expired"
    )
    [stored] = await token_rows(db, too_late.session_id)
    assert (stored["used_at"], stored["revoked_at"]) == (None, None)


async def test_a_spent_token_that_has_also_expired_is_refused_as_expired(
    db: Database, keys: KeySet, auth_settings: Settings, clock: ManualClock, maria: User
) -> None:
    first = await start_session(db, maria, keys, auth_settings)
    clock.advance(seconds=60)
    second = await rotated(db, first.refresh_token, keys, auth_settings)
    clock.advance(seconds=auth_settings.refresh_token_ttl_seconds - 60)

    replay = await rotate(db, first.refresh_token, keys, auth_settings)

    # Expiry is checked before reuse, so this replay does not end the session.
    assert replay.reason == "expired"
    assert (await rotate(db, second.refresh_token, keys, auth_settings)).reason is None


@pytest.mark.parametrize(
    "presented",
    [
        pytest.param(secrets.token_urlsafe(32), id="the right shape"),
        pytest.param("", id="empty"),
        pytest.param("garbage", id="one word"),
        pytest.param("refresh token with spaces", id="spaces"),
        pytest.param("é" * 43, id="not ASCII"),
        pytest.param("\ud800" * 43, id="not even text"),
        pytest.param("x" * 100_000, id="very long"),
    ],
)
async def test_a_token_nobody_was_given_is_unknown(
    db: Database, keys: KeySet, auth_settings: Settings, maria: User, presented: str
) -> None:
    await start_session(db, maria, keys, auth_settings)

    assert await rotate(db, presented, keys, auth_settings) == RefreshOutcome(
        tokens=None, user_id=None, session_id=None, reason="unknown"
    )
    async with db.transaction() as session:
        assert await count(session, "refresh_tokens") == 1


async def test_what_the_database_holds_is_of_no_use_as_a_token(
    db: Database, keys: KeySet, auth_settings: Settings, maria: User
) -> None:
    pair = await start_session(db, maria, keys, auth_settings)
    [stored] = await token_rows(db, pair.session_id)

    # Someone who reads the table learns hashes, and a hash is not accepted.
    assert (await rotate(db, str(stored["token_hash"]), keys, auth_settings)).reason == "unknown"
    assert (await rotate(db, pair.access_token, keys, auth_settings)).reason == "unknown"


async def test_a_closed_users_token_is_refused_and_not_spent(
    db: Database, keys: KeySet, auth_settings: Settings, maria: User
) -> None:
    first = await start_session(db, maria, keys, auth_settings)
    second = await rotated(db, first.refresh_token, keys, auth_settings)
    async with db.transaction() as session:
        await close_account(session, maria.id)

    for token in (second.refresh_token, first.refresh_token):
        assert await rotate(db, token, keys, auth_settings) == RefreshOutcome(
            tokens=None, user_id=maria.id, session_id=first.session_id, reason="closed"
        )
    stored = await token_rows(db, first.session_id)
    assert [(row["used_at"] is not None, row["revoked_at"]) for row in stored] == [
        (True, None),
        (False, None),
    ]


async def test_revoking_a_session_revokes_every_token_in_it_and_no_other(
    db: Database, keys: KeySet, auth_settings: Settings, clock: ManualClock, maria: User
) -> None:
    phone = await start_session(db, maria, keys, auth_settings)
    newest = await rotated(db, phone.refresh_token, keys, auth_settings)
    laptop = await start_session(db, maria, keys, auth_settings)
    clock.advance(minutes=5)

    async with db.transaction() as session:
        assert await identity.revoke_session(session, phone.session_id) == 2

    revoked_at = clock.now()
    assert [row["revoked_at"] for row in await token_rows(db, phone.session_id)] == [revoked_at] * 2
    assert [row["revoked_at"] for row in await token_rows(db, laptop.session_id)] == [None]
    assert await rotate(db, newest.refresh_token, keys, auth_settings) == RefreshOutcome(
        tokens=None, user_id=maria.id, session_id=phone.session_id, reason="revoked"
    )

    # Again, later: nothing left to revoke, and the first revocation's time stands.
    clock.advance(minutes=5)
    async with db.transaction() as session:
        assert await identity.revoke_session(session, phone.session_id) == 0
        assert await identity.revoke_session(session, new_id()) == 0
    assert [row["revoked_at"] for row in await token_rows(db, phone.session_id)] == [revoked_at] * 2


async def test_revoking_all_sessions_ends_every_session_of_that_user_and_nobody_elses(
    db: Database, keys: KeySet, auth_settings: Settings, maria: User
) -> None:
    async with db.transaction() as session:
        joao = await add_user(session, "joao")
    phone = await start_session(db, maria, keys, auth_settings)
    phone = await rotated(db, phone.refresh_token, keys, auth_settings)
    laptop = await start_session(db, maria, keys, auth_settings)
    other = await start_session(db, joao, keys, auth_settings)

    async with db.transaction() as session:
        assert await identity.revoke_all_sessions(session, maria.id) == 3
        assert await identity.revoke_all_sessions(session, maria.id) == 0

    assert (await rotate(db, phone.refresh_token, keys, auth_settings)).reason == "revoked"
    assert (await rotate(db, laptop.refresh_token, keys, auth_settings)).reason == "revoked"
    assert (await rotate(db, other.refresh_token, keys, auth_settings)).reason is None


async def test_a_rotation_that_commits_during_a_reuse_detection_does_not_keep_the_session_alive(
    db: Database, keys: KeySet, auth_settings: Settings, maria: User
) -> None:
    first = await start_session(db, maria, keys, auth_settings)
    second = await rotated(db, first.refresh_token, keys, auth_settings)

    async with db.transaction() as rotating:
        # A rotation of the newest token that has not committed yet. It holds that row.
        racing = await identity.rotate_refresh_token(
            rotating, second.refresh_token, keys=keys, settings=auth_settings
        )
        assert racing.tokens is not None
        # Meanwhile the spent token is replayed. Revoking the family waits for the row above.
        detection = asyncio.create_task(rotate(db, first.refresh_token, keys, auth_settings))
        await until_a_transaction_is_waiting_for_a_lock(db)
    assert (await detection).reason == "reuse_detected"

    # The revocation's statement started before the racing rotation committed, so it could
    # not see the token that rotation handed out. That row was left as it was.
    stored = await token_rows(db, first.session_id)
    assert [row["revoked_at"] is not None for row in stored] == [True, True, False]
    # It is refused all the same: one revoked token condemns the family.
    assert (await rotate(db, racing.tokens.refresh_token, keys, auth_settings)).reason == "revoked"


async def test_a_rotation_that_commits_during_a_logout_does_not_keep_the_session_alive(
    db: Database, keys: KeySet, auth_settings: Settings, maria: User
) -> None:
    first = await start_session(db, maria, keys, auth_settings)

    async with db.transaction() as rotating:
        racing = await identity.rotate_refresh_token(
            rotating, first.refresh_token, keys=keys, settings=auth_settings
        )
        assert racing.tokens is not None
        logout = asyncio.create_task(
            db.run(lambda session: identity.revoke_session(session, first.session_id))
        )
        await until_a_transaction_is_waiting_for_a_lock(db)
    # It revoked the one token it could see.
    assert await logout == 1

    assert (await rotate(db, racing.tokens.refresh_token, keys, auth_settings)).reason == "revoked"


async def test_the_database_refuses_two_tokens_with_one_hash_and_a_token_for_nobody(
    db: Database, maria: User
) -> None:
    insert = text(
        "INSERT INTO refresh_tokens (id, user_id, family_id, token_hash, issued_at, expires_at)"
        " VALUES (:id, :user, :family, :hash, :now, :now)"
    )

    def values(user: uuid.UUID) -> dict[str, object]:
        return {
            "id": new_id(),
            "user": user,
            "family": new_id(),
            "hash": "0" * 64,
            "now": utcnow(),
        }

    async with db.transaction() as session:
        await session.execute(insert, values(maria.id))

    with pytest.raises(DBAPIError) as duplicate:
        async with db.transaction() as session:
            await session.execute(insert, values(maria.id))
    assert sqlstate_of(duplicate.value) == UNIQUE_VIOLATION
    assert constraint_of(duplicate.value) == "uq_refresh_tokens_token_hash"

    with pytest.raises(DBAPIError) as orphan:
        async with db.transaction() as session:
            await session.execute(insert, values(new_id()) | {"hash": "1" * 64})
    assert sqlstate_of(orphan.value) == FOREIGN_KEY_VIOLATION
    assert constraint_of(orphan.value) == "fk_refresh_tokens_user_id_users"


# --- the revocation hint in Redis ------------------------------------------------------------


def unavailable(use: str) -> float:
    return REGISTRY.get_sample_value("corridor_redis_unavailable_total", {"use": use}) or 0.0


async def test_a_session_marked_revoked_reads_as_revoked_until_the_mark_expires(
    redis: RedisStore,
) -> None:
    logged_out, still_in = new_id(), new_id()
    assert await is_session_revoked(redis, logged_out) is False

    await mark_session_revoked(redis, logged_out, ttl_seconds=900)

    assert await is_session_revoked(redis, logged_out) is True
    assert await is_session_revoked(redis, still_in) is False
    # The mark outlives the access tokens it is there to stop, and no longer than that.
    key = redis.key("revoked", "sid", str(logged_out))
    assert 890 < await redis.client.ttl(key) <= 900

    await mark_session_revoked(redis, still_in, ttl_seconds=30)
    assert 20 < await redis.client.ttl(redis.key("revoked", "sid", str(still_in))) <= 30


async def test_a_revocation_mark_is_gone_once_its_lifetime_has_passed(redis: RedisStore) -> None:
    await mark_session_revoked(redis, SESSION_ID, ttl_seconds=1)
    assert await is_session_revoked(redis, SESSION_ID) is True

    # Redis expires keys by its own clock, which the application clock does not move, so
    # this is the one place that waits for real time: a second, and a bounded margin.
    for _ in range(100):
        if not await is_session_revoked(redis, SESSION_ID):
            break
        await asyncio.sleep(0.05)

    assert await is_session_revoked(redis, SESSION_ID) is False


async def test_when_redis_is_unreachable_a_session_reads_as_not_revoked_and_it_is_counted(
    settings: Settings,
) -> None:
    # Port 1 on localhost: nothing listens there, so the connection is refused at once.
    dead = settings.model_copy(update={"redis_url": SecretStr("redis://127.0.0.1:1/0")})
    store = RedisStore(create_redis(dead), "unused:")
    before = unavailable("session_revocation")
    try:
        await mark_session_revoked(store, SESSION_ID, ttl_seconds=900)
        assert await is_session_revoked(store, SESSION_ID) is False
    finally:
        await store.close()

    assert unavailable("session_revocation") == before + 2


@pytest.mark.parametrize("ttl_seconds", [0, -1])
async def test_a_revocation_mark_needs_a_lifetime(redis: RedisStore, ttl_seconds: int) -> None:
    # Redis refuses such an expiry, and that refusal would pass for Redis being down.
    with pytest.raises(ValueError, match="ttl_seconds"):
        await mark_session_revoked(redis, SESSION_ID, ttl_seconds=ttl_seconds)

    assert await is_session_revoked(redis, SESSION_ID) is False


# --- what a token says against what is true now ----------------------------------------------


def claims_of(user: User, clock: ManualClock, *, role: str | None = None) -> AccessClaims:
    """What a token issued to the user at this moment would say."""
    issued_at = clock.now().replace(microsecond=0)
    return AccessClaims(
        user_id=user.id,
        session_id=new_id(),
        role=role or user.role,  # type: ignore[arg-type]
        scope="*",
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=15),
        token_id=str(new_id()),
    )


async def accepted(db: Database, claims: AccessClaims) -> bool:
    async with db.transaction() as session:
        try:
            await identity.check_access(session, claims)
        except InvalidToken:
            return False
    return True


async def test_the_token_of_a_user_in_good_standing_is_accepted(
    db: Database, clock: ManualClock, maria: User
) -> None:
    assert await accepted(db, claims_of(maria, clock)) is True


async def test_the_token_of_a_restricted_user_is_accepted(
    db: Database, clock: ManualClock, maria: User
) -> None:
    claims = claims_of(maria, clock)
    async with db.transaction() as session:
        await identity.restrict_user(session, maria.id, "under review")

    assert await accepted(db, claims) is True


async def test_the_token_of_a_closed_account_is_refused(
    db: Database, clock: ManualClock, maria: User
) -> None:
    claims = claims_of(maria, clock)
    async with db.transaction() as session:
        await close_account(session, maria.id)

    assert await accepted(db, claims) is False


async def test_a_token_for_nobody_is_refused(db: Database, clock: ManualClock, maria: User) -> None:
    ghost = dataclasses.replace(maria, id=new_id())

    assert await accepted(db, claims_of(ghost, clock)) is False


@pytest.mark.parametrize(("held", "claimed"), [("user", "admin"), ("admin", "user")])
async def test_a_token_that_carries_a_role_its_user_does_not_hold_is_refused(
    db: Database, clock: ManualClock, held: str, claimed: str
) -> None:
    async with db.transaction() as session:
        user = await add_user(session, "someone", role=held)  # type: ignore[arg-type]

    assert await accepted(db, claims_of(user, clock, role=claimed)) is False
    assert await accepted(db, claims_of(user, clock)) is True


async def test_changing_a_role_ends_every_token_issued_up_to_that_second(
    db: Database, clock: ManualClock, maria: User
) -> None:
    # A token's issue time is in whole seconds, rounded down. One issued a moment before
    # the change says a time at or before the change's own second, and has to go.
    clock.advance(seconds=0.2)
    issued_just_before = claims_of(maria, clock, role="admin")
    clock.advance(seconds=0.3)
    async with db.transaction() as session:
        await identity.set_role(session, maria.id, "admin")
    promoted = dataclasses.replace(maria, role="admin")

    rest_of_the_second = claims_of(promoted, clock)
    clock.advance(seconds=0.5)
    next_second = claims_of(promoted, clock)

    assert await accepted(db, issued_just_before) is False
    # The price of that: a token issued in what is left of the second is refused as well.
    assert await accepted(db, rest_of_the_second) is False
    assert await accepted(db, next_second) is True


async def test_checking_a_token_is_one_read_by_primary_key(
    db: Database, clock: ManualClock, maria: User
) -> None:
    claims = claims_of(maria, clock)
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    event.listen(db.engine.sync_engine, "before_cursor_execute", record)
    try:
        async with db.transaction() as session:
            await identity.check_access(session, claims)
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", record)

    reads = [statement for statement in statements if "users" in statement]
    assert len(reads) == 1
    assert "WHERE users.id =" in reads[0]
    assert [s for s in statements if s.lstrip().upper().startswith(("UPDATE", "INSERT"))] == []
