"""Request and response bodies.

A field that carries a secret is a ``SecretStr``, so that a body which ends up in a log
line, an error or a debugger shows asterisks where the secret was.
"""

import re
import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    StringConstraints,
)

from corridor import identity

# Far above any real value. These bound the work an anonymous request can ask for; what a
# password or a token may be is decided by the code that checks it.
_MAX_EMAIL_LENGTH = 320
_MAX_SECRET_LENGTH = 1024

# The C0 control characters, U+0000 to U+001F, tab and newline among them.
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f]")


def _refuse_control_characters(value: object) -> object:
    # Before any other rule, so that stripping whitespace cannot hide one. The message
    # does not repeat what was sent.
    if isinstance(value, str) and _CONTROL_CHARACTER.search(value) is not None:
        raise ValueError("must not contain control characters")
    return value


# What every free-text field of a request body is made of. PostgreSQL refuses a NUL in
# text, so one that got as far as a query would be a failure of ours and not a refusal;
# the other control characters have no place in a name, a handle or a memo either.
_NO_CONTROL_CHARACTERS = BeforeValidator(_refuse_control_characters)
Text = Annotated[str, _NO_CONTROL_CHARACTERS]

# Text, with its own rules stated on the string itself: written after the check, they
# would be applied around it and the least length would be measured before the stripping.
DisplayName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    _NO_CONTROL_CHARACTERS,
]


class _Request(BaseModel):
    # An unknown field is refused rather than dropped: a client that misspells one learns
    # of it at once.
    model_config = ConfigDict(extra="forbid", frozen=True)


class RegisterRequest(_Request):
    email: EmailStr
    # The handle's own rule is identity's; this only bounds its length.
    handle: Annotated[Text, Field(max_length=64)]
    display_name: DisplayName
    # Its least length is checked by identity, whose refusal says what the rule is. The
    # most is here because it bounds the hashing an anonymous request can ask for.
    password: Annotated[SecretStr, Field(max_length=identity.MAX_PASSWORD_LENGTH)]


class LoginRequest(_Request):
    # Not validated as an address: whatever is typed here that is not a registered email
    # gets the same answer as a wrong password.
    email: Annotated[Text, Field(max_length=_MAX_EMAIL_LENGTH)]
    password: Annotated[SecretStr, Field(max_length=_MAX_SECRET_LENGTH)]


class RefreshRequest(_Request):
    refresh_token: Annotated[SecretStr, Field(max_length=_MAX_SECRET_LENGTH)]


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    handle: str
    display_name: str
    role: identity.Role
    kyc_tier: int
    status: identity.UserStatus
    created_at: datetime

    @classmethod
    def of(cls, user: identity.User) -> Self:
        return cls(
            id=user.id,
            email=user.email,
            handle=user.handle,
            display_name=user.display_name,
            role=user.role,
            kyc_tier=user.kyc_tier,
            status=user.status,
            created_at=user.created_at,
        )


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 - the name of a scheme, not a secret
    # Seconds until the access token expires.
    expires_in: int

    @classmethod
    def of(cls, pair: identity.TokenPair) -> Self:
        return cls(
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            expires_in=pair.expires_in,
        )
