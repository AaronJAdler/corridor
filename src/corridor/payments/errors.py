"""What the payments module refuses, and how."""

from typing import Final

from corridor.platform.errors import (
    Conflict,
    InvalidRequest,
    NotFound,
    PermissionDenied,
    ServiceUnavailable,
)
from corridor.risk import Denied

# The table's check constraint states the same limit.
MAX_MEMO_LENGTH: Final = 140


class RecipientNotFound(NotFound):
    """Nobody answers to what the sender typed. A closed account is nobody."""

    code = "recipient_not_found"
    title = "Recipient not found"

    def __init__(self) -> None:
        super().__init__("There is no such recipient.")


class TransferNotFound(NotFound):
    """There is no such transfer, or there is and it is not this user's to see. The two
    are never told apart."""

    code = "transfer_not_found"
    title = "Transfer not found"

    def __init__(self) -> None:
        super().__init__("There is no such transfer.")


class CannotTransferToSelf(InvalidRequest):
    """The recipient is the sender. Nothing would move, and a fee would still be charged."""

    code = "transfer_to_self"
    title = "Cannot transfer to yourself"

    def __init__(self) -> None:
        super().__init__("The recipient of a transfer cannot be its sender.")


class InvalidMemo(InvalidRequest):
    code = "invalid_memo"
    title = "Invalid memo"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or f"A memo is at most {MAX_MEMO_LENGTH} characters.", field="memo")


class DuplicateTransfer(Exception):
    """A transfer id was used for a second transfer. A bug in the caller, which makes a new
    id for each attempt, and not something a client can cause."""


class ProviderUnavailable(ServiceUnavailable):
    """The bank or the custodian could not be asked, or did not give an answer that can be
    relied on. Nothing was changed here, and asking again later is safe."""

    code = "provider_unavailable"
    title = "Provider unavailable"

    def __init__(self) -> None:
        super().__init__("This cannot be done at the moment. Try again shortly.")


class AccountNotActive(Denied):
    """A restricted or closed account asked for something new at a provider: an account to
    deposit into, or one to withdraw to. Answered as risk answers a movement of money, and
    like it never says why."""

    code = "user_restricted"
    title = "Account restricted"

    def __init__(self) -> None:
        super().__init__("This account cannot do this at the moment.")


class DepositNotInSuspense(Conflict):
    """An operator asked for a deposit to be released from suspense that is not there: it
    was released already, or its bank took it back."""

    code = "deposit_not_in_suspense"
    title = "Deposit is not in suspense"

    def __init__(self) -> None:
        super().__init__("This deposit is no longer in suspense.")


class DepositOwnerClosed(Conflict):
    """An operator asked for a deposit in suspense to be credited to a closed account.
    Nothing is paid into one: the money would be out of everybody's reach."""

    code = "deposit_owner_closed"
    title = "Account is closed"

    def __init__(self) -> None:
        super().__init__("A deposit cannot be released to a closed account.")


class DepositNotFound(NotFound):
    """There is no such deposit, or there is and it is not this user's to see. The two are
    never told apart."""

    code = "deposit_not_found"
    title = "Deposit not found"

    def __init__(self) -> None:
        super().__init__("There is no such deposit.")


class MalformedProviderEvent(ValueError):
    """What a provider's event carried is not what the provider contract says it carries.

    Not a ``DomainError``: no client of the API caused it. It names the fields that were
    wrong and never what they held, so that it can be logged and stored as it is.
    """


class ProviderEventMismatch(Exception):
    """An event is well-formed and disagrees with what Corridor recorded: another asset or
    another amount than the deposit or the withdrawal it names. Money never moves on one."""


class DepositNotReceived(Exception):
    """A return found its deposit unrecorded and, while it was recording it as returned,
    the deposit was received by another transaction. Raised so that whoever delivers the
    return tries again, and this time finds the deposit to reverse."""


class InvalidBeneficiaryAccount(InvalidRequest):
    """The bank does not accept these account details for the asset's rail. It never says
    what was sent."""

    code = "invalid_account"
    title = "Invalid account"

    def __init__(self) -> None:
        super().__init__("These are not valid account details for this asset.")


class BeneficiaryRejected(InvalidRequest):
    """The bank refused to register the account, for a reason of its own."""

    code = "beneficiary_rejected"
    title = "Beneficiary rejected"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or "This account could not be registered.")


class UnsupportedBeneficiaryAsset(BeneficiaryRejected):
    """Only an asset that moves by bank has beneficiaries."""

    code = "unsupported_asset"
    title = "Unsupported asset"

    def __init__(self) -> None:
        super().__init__("Bank accounts can be saved for fiat assets only.")


class BeneficiaryLimitReached(Conflict):
    """The user has as many saved accounts as a user may have. They are never removed, so
    this is final for the user and says how many that is."""

    code = "beneficiary_limit_reached"
    title = "Beneficiary limit reached"

    def __init__(self, limit: int) -> None:
        super().__init__(f"An account can have at most {limit} saved bank accounts.", limit=limit)


class BeneficiaryKeyReused(InvalidRequest):
    """The idempotency key already registered a different account."""

    code = "idempotency_key_reused"
    title = "Idempotency key reused"

    def __init__(self) -> None:
        super().__init__("This idempotency key was already used for a different request.")


class BeneficiaryNotFound(NotFound):
    """There is no such beneficiary, or there is and it is another user's. The two are
    never told apart."""

    code = "beneficiary_not_found"
    title = "Beneficiary not found"

    def __init__(self) -> None:
        super().__init__("There is no such beneficiary.")


class BeneficiaryAssetMismatch(InvalidRequest):
    code = "beneficiary_asset_mismatch"
    title = "Beneficiary is in another asset"

    def __init__(self) -> None:
        super().__init__("This beneficiary receives a different asset.", field="beneficiary_id")


class InvalidWithdrawalTarget(InvalidRequest):
    """A bank asset goes to a saved beneficiary and a stablecoin to an address: one of the
    two, and the one that fits the asset."""

    code = "invalid_withdrawal_target"
    title = "Invalid withdrawal target"


class InvalidAddress(InvalidRequest):
    code = "invalid_address"
    title = "Invalid address"

    def __init__(self) -> None:
        super().__init__("That is not a valid address on this network.", field="to_address")


class WithdrawalNotFound(NotFound):
    """There is no such withdrawal, or there is and it is not this user's to see."""

    code = "withdrawal_not_found"
    title = "Withdrawal not found"

    def __init__(self) -> None:
        super().__init__("There is no such withdrawal.")


class WithdrawalNotAgents(PermissionDenied):
    """An agent tried to call back a withdrawal that it did not ask for itself: its
    owner's own, or another agent's. Those are the owner's to cancel."""

    code = "withdrawal_not_agents"
    title = "Withdrawal is not this agent's"

    def __init__(self) -> None:
        super().__init__("An agent can cancel only a withdrawal it asked for.")


class WithdrawalNotCancelable(Conflict):
    """The withdrawal has left the state in which its user can still call it back."""

    code = "withdrawal_not_cancelable"
    title = "Withdrawal cannot be canceled"

    def __init__(self) -> None:
        super().__init__("This withdrawal can no longer be canceled.")


class DuplicateWithdrawal(Exception):
    """A withdrawal id was used for a second withdrawal. A bug in the caller, which makes a
    new id for each attempt, and not something a client can cause."""
