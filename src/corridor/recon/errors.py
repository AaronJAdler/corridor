"""What reconciliation refuses, and how."""

from corridor.platform.errors import Conflict, InvalidRequest, NotFound


class BreakNotFound(NotFound):
    code = "recon_break_not_found"
    title = "Reconciliation break not found"


class BreakNotOpen(Conflict):
    """The break was resolved already, by the repair or by somebody else."""

    code = "recon_break_not_open"
    title = "Reconciliation break is not open"


class InvalidNote(InvalidRequest):
    code = "invalid_note"
    title = "Invalid note"
