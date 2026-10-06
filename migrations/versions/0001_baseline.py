"""Baseline: privileges for the application role, and the append-only guard.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    app_role = context.config.attributes["app_role"]
    for statement in (
        # The application role may read and write rows in every table the owner creates from
        # here on. It can never create, alter or drop anything. Revisions for append-only
        # tables then take UPDATE and DELETE away again.
        f'GRANT USAGE ON SCHEMA public TO "{app_role}"',
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{app_role}"',
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO "{app_role}"',
        # A trigger function for tables whose rows are never changed once written. The
        # privilege removal is the first barrier; this is the one that also stops the owner
        # and anyone holding a broader role by mistake.
        # The search path is pinned, with the temporary schema last, as on every function:
        # a session's temporary objects must never be found ahead of the real ones.
        """
        CREATE FUNCTION forbid_mutation() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public, pg_temp
        AS $$
        BEGIN
            RAISE EXCEPTION '% on % is not allowed: the table is append-only', TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'CR001';
        END
        $$
        """,
    ):
        op.execute(statement)


def downgrade() -> None:
    app_role = context.config.attributes["app_role"]
    for statement in (
        "DROP FUNCTION forbid_mutation()",
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM "{app_role}"',
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM "{app_role}"',
        f'REVOKE USAGE ON SCHEMA public FROM "{app_role}"',
    ):
        op.execute(statement)
