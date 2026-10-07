"""What the payments module refuses, and how."""

from typing import Final

from corridor.platform.errors import InvalidRequest, NotFound

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

    def __init__(self) -> None:
        super().__init__(f"A memo is at most {MAX_MEMO_LENGTH} characters.", field="memo")


class DuplicateTransfer(Exception):
    """A transfer id was used for a second transfer. A bug in the caller, which makes a new
    id for each attempt, and not something a client can cause."""
