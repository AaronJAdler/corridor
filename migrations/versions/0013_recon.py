"""Reconciliation: runs and breaks.

A run compares what a provider says happened with what Corridor recorded, and a break is
one thing the two disagree about. Breaks stay until somebody, or the repair, resolves them.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE recon_runs (
    id             uuid        NOT NULL,
    window_start   timestamptz NOT NULL,
    window_end     timestamptz NOT NULL,
    -- 'incomplete' when a provider could not be read, so part of the window was not compared.
    status         text        NOT NULL,
    -- How many disagreements the run saw, and how many of them had no open break yet.
    breaks_found   integer     NOT NULL,
    breaks_opened  integer     NOT NULL,
    started_at     timestamptz NOT NULL,
    finished_at    timestamptz NOT NULL,
    CONSTRAINT pk_recon_runs PRIMARY KEY (id),
    CONSTRAINT ck_recon_runs_status CHECK (status IN ('completed', 'incomplete')),
    CONSTRAINT ck_recon_runs_window CHECK (window_start < window_end),
    CONSTRAINT ck_recon_runs_counts CHECK (breaks_opened >= 0 AND breaks_found >= breaks_opened)
);

CREATE TABLE recon_breaks (
    id            uuid        NOT NULL,
    -- The run that first saw it. Later runs that see it again leave it as it is.
    run_id        uuid        NOT NULL,
    kind          text        NOT NULL,
    provider      text        NOT NULL,
    -- The provider's id for the deposit or the payout; the asset code for a balance.
    provider_ref  text        NOT NULL,
    asset_code    text        NOT NULL,
    -- What Corridor recorded and what the provider reports, in minor units. Null on the
    -- side that has nothing. A balance may be negative.
    expected      numeric(38,0),
    actual        numeric(38,0),
    status        text        NOT NULL,
    note          text,
    -- 'system' for a repair, or the id of the admin who resolved it.
    resolved_by   text,
    created_at    timestamptz NOT NULL,
    resolved_at   timestamptz,
    CONSTRAINT pk_recon_breaks PRIMARY KEY (id),
    -- Deferred: a run's row is written once, when the run ends and its counts are known,
    -- which is after its breaks.
    CONSTRAINT fk_recon_breaks_run_id_recon_runs FOREIGN KEY (run_id) REFERENCES recon_runs (id)
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_recon_breaks_kind CHECK (kind IN ('missing_deposit', 'unknown_deposit',
        'amount_mismatch', 'missing_payout_result', 'unknown_payout', 'settlement_balance')),
    CONSTRAINT ck_recon_breaks_status CHECK (status IN ('open', 'resolved')),
    CONSTRAINT ck_recon_breaks_resolution CHECK (
        (status = 'resolved') = (resolved_by IS NOT NULL)
        AND (status = 'resolved') = (resolved_at IS NOT NULL)),
    CONSTRAINT ck_recon_breaks_note CHECK (char_length(note) <= 500)
);

-- One open break per disagreement: a run that sees it again does not open it again.
CREATE UNIQUE INDEX uq_recon_breaks_open ON recon_breaks (kind, provider, provider_ref)
    WHERE status = 'open';

CREATE INDEX ix_recon_breaks_status_id ON recon_breaks (status, id);

CREATE INDEX ix_recon_breaks_run_id ON recon_breaks (run_id)
"""

# A run is written once, when it ends. A break is resolved and never removed.
REVOKES = """
REVOKE UPDATE, DELETE ON recon_runs FROM "{app_role}";
REVOKE DELETE ON recon_breaks FROM "{app_role}"
"""


def _statements(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";\n") if statement.strip()]


def upgrade() -> None:
    app_role = context.config.attributes["app_role"]
    for statement in _statements(TABLES):
        op.execute(statement)
    for statement in _statements(REVOKES.format(app_role=app_role)):
        op.execute(statement)


def downgrade() -> None:
    for statement in ("DROP TABLE recon_breaks", "DROP TABLE recon_runs"):
        op.execute(statement)
