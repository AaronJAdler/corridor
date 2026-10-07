"""Reconciliation: comparing what the providers report with what Corridor recorded.

Other modules use the names exported here and nothing else from this package.
"""

from corridor.recon.breaks import MAX_NOTE_LENGTH, list_breaks, list_runs, resolve_break
from corridor.recon.errors import BreakNotFound, BreakNotOpen, InvalidNote
from corridor.recon.service import LOOKBACK, run
from corridor.recon.types import SYSTEM, Break, BreakKind, BreakStatus, Run, RunResult, RunStatus

__all__ = [
    "LOOKBACK",
    "MAX_NOTE_LENGTH",
    "SYSTEM",
    "Break",
    "BreakKind",
    "BreakNotFound",
    "BreakNotOpen",
    "BreakStatus",
    "InvalidNote",
    "Run",
    "RunResult",
    "RunStatus",
    "list_breaks",
    "list_runs",
    "resolve_break",
    "run",
]
