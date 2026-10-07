"""Signing keys for access tokens.

Tokens are signed with ES256, so every key is on the P-256 curve and no other kind is
accepted. A key is named by the RFC 7638 thumbprint of its public half. The name is derived
from the key, so there is nothing to configure and nothing that can get out of step.
"""

import base64
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from corridor.identity.errors import ConfigurationError
from corridor.platform.config import Settings

ALGORITHM: Final = "ES256"

# A P-256 coordinate is always written as 32 bytes, leading zeros included.
_COORDINATE_BYTES: Final = 32

_SIGNING_KEY: Final = "CORRIDOR_JWT_SIGNING_KEY"
_SIGNING_KEY_FILE: Final = "CORRIDOR_JWT_SIGNING_KEY_FILE"
_ADDITIONAL_PUBLIC_KEYS: Final = "CORRIDOR_JWT_ADDITIONAL_PUBLIC_KEYS"


@dataclass(frozen=True, slots=True)
class KeySet:
    """The key that signs access tokens, and every key that may verify one."""

    signing_kid: str
    signing_key: ec.EllipticCurvePrivateKey = field(repr=False)
    # By key id. Holds the signing key's own public half, and the public halves of retired
    # signing keys, whose tokens must go on verifying until they expire.
    public_keys: Mapping[str, ec.EllipticCurvePublicKey]

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        """The verification keys as a JWK Set. Built from public keys only, so it cannot
        carry a private parameter."""
        return {
            "keys": [
                {**_public_jwk(key), "kid": kid, "use": "sig", "alg": ALGORITHM}
                for kid, key in self.public_keys.items()
            ]
        }


def key_id(public_key: ec.EllipticCurvePublicKey) -> str:
    """The RFC 7638 thumbprint of a key: SHA-256 over its required JWK members, in
    alphabetical order with no whitespace, as unpadded base64url."""
    canonical = json.dumps(_public_jwk(public_key), sort_keys=True, separators=(",", ":"))
    return _b64url(hashlib.sha256(canonical.encode("ascii")).digest())


def load_keyset(settings: Settings) -> KeySet:
    """Build the key set from configuration.

    A wrong configuration stops the process from starting. The message names the setting
    and says what is wrong with it, and never repeats any part of a key.
    """
    inline, path = settings.jwt_signing_key, settings.jwt_signing_key_file
    if inline is not None and path is None:
        signing_key = _load_private_key(inline.get_secret_value().encode(), _SIGNING_KEY)
    elif path is not None and inline is None:
        try:
            pem = path.read_bytes()
        except OSError as error:
            raise ConfigurationError(
                f"{_SIGNING_KEY_FILE}: {path} could not be read ({error.strerror})."
            ) from None
        signing_key = _load_private_key(pem, _SIGNING_KEY_FILE)
    else:
        raise ConfigurationError(
            f"Exactly one of {_SIGNING_KEY} and {_SIGNING_KEY_FILE} must be set, and "
            + ("both are." if inline is not None else "neither is.")
        )

    signing_kid = key_id(signing_key.public_key())
    public_keys = {signing_kid: signing_key.public_key()}
    for position, pem_text in enumerate(settings.jwt_additional_public_keys):
        public_key = _load_public_key(pem_text.encode(), f"{_ADDITIONAL_PUBLIC_KEYS}[{position}]")
        public_keys.setdefault(key_id(public_key), public_key)
    return KeySet(
        signing_kid=signing_kid,
        signing_key=signing_key,
        public_keys=MappingProxyType(public_keys),
    )


def generate_private_key_pem() -> str:
    """A new signing key, as unencrypted PKCS#8 PEM."""
    return _private_pem(ec.generate_private_key(ec.SECP256R1()))


# Readable by the owner alone.
PRIVATE_KEY_MODE: Final = 0o600


def write_keypair(directory: Path, *, mode: int = PRIVATE_KEY_MODE) -> tuple[str, Path]:
    """Generate a signing key and write it to ``directory`` as ``<kid>.pem``, with its
    public half beside it as ``<kid>.pub.pem``. Returns the key id and the private path.

    ``mode`` is the private key's permission bits. Anything wider than the default is for
    a key that a process running as another user has to read, such as a container that
    is given the file through a bind mount.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    kid = key_id(key.public_key())
    directory.mkdir(parents=True, exist_ok=True)

    private_path = directory / f"{kid}.pem"
    # Created with its final mode, readable by the owner alone where the platform has such
    # a thing, rather than created open and narrowed afterwards. An existing file is never
    # overwritten.
    descriptor = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    # The mode given at creation is narrowed by the process's umask. Set again, so that
    # the file has the mode that was asked for and not one that depends on the shell.
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, mode)
    with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
        handle.write(_private_pem(key))
    (directory / f"{kid}.pub.pem").write_text(
        _public_pem(key.public_key()), encoding="ascii", newline="\n"
    )
    return kid, private_path


def _load_private_key(pem: bytes, setting: str) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except ValueError, TypeError, UnsupportedAlgorithm:
        # ``from None``: the library's error describes the key, and is not passed on.
        raise ConfigurationError(f"{setting} is not an unencrypted PEM private key.") from None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ConfigurationError(f"{setting} must be a P-256 key: tokens are signed with ES256.")
    return key


def _load_public_key(pem: bytes, setting: str) -> ec.EllipticCurvePublicKey:
    try:
        key = serialization.load_pem_public_key(pem)
    except ValueError, UnsupportedAlgorithm:
        raise ConfigurationError(f"{setting} is not a PEM public key.") from None
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ConfigurationError(f"{setting} must be a P-256 key: tokens are signed with ES256.")
    return key


def _public_jwk(key: ec.EllipticCurvePublicKey) -> dict[str, str]:
    if not isinstance(key.curve, ec.SECP256R1):
        # Another 256-bit curve would otherwise be described, wrongly, as P-256.
        raise ValueError("only P-256 keys are supported")
    numbers = key.public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(numbers.x.to_bytes(_COORDINATE_BYTES, "big")),
        "y": _b64url(numbers.y.to_bytes(_COORDINATE_BYTES, "big")),
    }


def _private_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def _public_pem(key: ec.EllipticCurvePublicKey) -> str:
    return key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
