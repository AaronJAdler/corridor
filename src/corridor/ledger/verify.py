"""The ledger verifier: recompute everything the ledger promises and report what differs.

The application refuses to create a violation and the database refuses to store one. This
is the third layer: it assumes both of those failed and looks anyway. It reads only, so it
is safe to run against a live system. A clean ledger produces no findings.
"""

from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from corridor.ledger.types import CHART

# An account's balance is the sum of postings on its normal side less the sum on the other.
_SIGNED: Final = "CASE WHEN p.direction = a.normal_side THEN p.amount ELSE -p.amount END"

# The chart of accounts as rows, for the check that compares every account with it. Built
# from the chart the code posts by, so the two cannot drift apart.
_CHART_ROWS: Final = ", ".join(
    f"('{kind.value}', '{spec.category}', '{spec.normal_side.value}',"
    f" {'true' if spec.constrained else 'false'}, '{spec.scope}')"
    for kind, spec in CHART.items()
)

# Each check is a query that returns one row per violation: the id it concerns and a
# description. Each query stands alone, so each reads one consistent snapshot.
CHECKS: Final[dict[str, str]] = {
    "unbalanced_entry": """
        SELECT p.entry_id::text AS subject,
               'debits exceed credits by '
                   || sum(CASE p.direction WHEN 'D' THEN p.amount ELSE -p.amount END)
                   || ' in ' || p.asset_code AS detail
          FROM postings p
         GROUP BY p.entry_id, p.asset_code
        HAVING sum(CASE p.direction WHEN 'D' THEN p.amount ELSE -p.amount END) <> 0
    """,
    "too_few_postings": """
        SELECT e.id::text AS subject, 'entry has ' || count(p.seq) || ' posting(s)' AS detail
          FROM journal_entries e
          LEFT JOIN postings p ON p.entry_id = e.id
         GROUP BY e.id
        HAVING count(p.seq) < 2
    """,
    "negative_balance": """
        SELECT b.account_id::text AS subject, 'cached balance is ' || b.balance AS detail
          FROM account_balances b
         WHERE b.balance < 0
    """,
    "balance_mismatch": f"""
        SELECT b.account_id::text AS subject,
               'cached balance is ' || b.balance || ' but postings sum to '
                   || COALESCE(derived.total, 0) AS detail
          FROM account_balances b
          LEFT JOIN (
                SELECT p.account_id, sum({_SIGNED}) AS total
                  FROM postings p
                  JOIN ledger_accounts a ON a.id = p.account_id
                 GROUP BY p.account_id
               ) derived ON derived.account_id = b.account_id
         WHERE b.balance <> COALESCE(derived.total, 0)
    """,  # noqa: S608 - built from a constant
    # Suspense has no cached balance to refuse an overdraft, so its postings are summed
    # here. Less than nothing in it means money left it that never arrived in it: a
    # deposit paid out twice.
    "negative_suspense": f"""
        SELECT a.id::text AS subject,
               'suspense holds ' || sum({_SIGNED}) || ' in ' || a.asset_code AS detail
          FROM ledger_accounts a
          JOIN postings p ON p.account_id = a.id
         WHERE a.kind = 'suspense'
         GROUP BY a.id, a.asset_code
        HAVING sum({_SIGNED}) < 0
    """,  # noqa: S608 - built from a constant
    "missing_balance_row": """
        SELECT a.id::text AS subject, a.kind || ' account has no cached balance' AS detail
          FROM ledger_accounts a
          LEFT JOIN account_balances b ON b.account_id = a.id
         WHERE a.is_constrained AND b.account_id IS NULL
    """,
    "unexpected_balance_row": """
        SELECT a.id::text AS subject, a.kind || ' account should not have a cached balance' AS detail
          FROM ledger_accounts a
          JOIN account_balances b ON b.account_id = a.id
         WHERE NOT a.is_constrained
    """,
    "broken_balance_chain": f"""
        SELECT chain.account_id::text AS subject,
               'posting ' || chain.seq || ' records balance '
                   || COALESCE(chain.balance_after::text, 'null')
                   || ' where the postings before it give ' || chain.expected AS detail
          FROM (
                SELECT p.seq, p.account_id, p.balance_after,
                       sum({_SIGNED}) OVER (PARTITION BY p.account_id ORDER BY p.seq) AS expected
                  FROM postings p
                  JOIN ledger_accounts a ON a.id = p.account_id
                 WHERE a.is_constrained
               ) chain
         WHERE chain.balance_after IS DISTINCT FROM chain.expected
    """,  # noqa: S608 - built from a constant
    "stray_balance_after": """
        SELECT p.account_id::text AS subject,
               'posting ' || p.seq || ' on an unconstrained account records a balance' AS detail
          FROM postings p
          JOIN ledger_accounts a ON a.id = p.account_id
         WHERE NOT a.is_constrained AND p.balance_after IS NOT NULL
    """,
    "stale_balance_pointer": """
        SELECT b.account_id::text AS subject,
               'balance row points at posting ' || b.last_posting_seq
                   || ' but the latest is ' || COALESCE(latest.seq, 0) AS detail
          FROM account_balances b
          LEFT JOIN (
                SELECT account_id, max(seq) AS seq FROM postings GROUP BY account_id
               ) latest ON latest.account_id = b.account_id
         WHERE b.last_posting_seq <> COALESCE(latest.seq, 0)
    """,
    # The database holds an account to the chart, and lets nobody change one. An account
    # that differs all the same gives every posting on it another meaning: a normal side
    # turned over turns the sign of its balance.
    "account_off_chart": f"""
        SELECT a.id::text AS subject,
               a.kind || ' account is ' || a.category || ', normal side ' || a.normal_side
                   || ', which is not what the chart of accounts gives its kind' AS detail
          FROM ledger_accounts a
          LEFT JOIN (VALUES {_CHART_ROWS})
               AS chart (kind, category, normal_side, is_constrained, scope)
            ON chart.kind = a.kind
         WHERE chart.kind IS NULL
            OR a.category <> chart.category
            OR a.normal_side <> chart.normal_side
            OR a.is_constrained <> chart.is_constrained
            OR (a.owner_id IS NOT NULL) <> (chart.scope = 'user')
            OR (a.provider IS NOT NULL) <> (chart.scope = 'provider')
    """,  # noqa: S608 - built from a constant
    # A reversal is its original with every posting turned over, and nothing else. An
    # account appears once in an entry, so the two are compared posting for posting.
    "reversal_mismatch": """
        SELECT r.id::text AS subject,
               'the postings of this reversal are not the mirror image of entry '
                   || r.reverses_entry_id AS detail
          FROM journal_entries r
         WHERE r.reverses_entry_id IS NOT NULL
           AND EXISTS (
                SELECT 1
                  FROM (SELECT account_id, direction, amount
                          FROM postings
                         WHERE entry_id = r.reverses_entry_id) original
                  FULL JOIN (SELECT account_id,
                                    CASE direction WHEN 'D' THEN 'C' ELSE 'D' END AS direction,
                                    amount
                               FROM postings
                              WHERE entry_id = r.id) mirrored
                    USING (account_id, direction, amount)
                 WHERE original.account_id IS NULL OR mirrored.account_id IS NULL
               )
    """,
}


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    subject: str
    detail: str

    def __str__(self) -> str:
        return f"{self.check}: {self.subject}: {self.detail}"


async def verify(session: AsyncSession, *, limit_per_check: int = 100) -> list[Finding]:
    """Run every check and return what it found. An empty list means the ledger is sound."""
    findings: list[Finding] = []
    for name, query in CHECKS.items():
        rows = await session.execute(
            text(f"SELECT subject, detail FROM ({query}) violations ORDER BY subject LIMIT :limit"),  # noqa: S608
            {"limit": limit_per_check},
        )
        findings.extend(Finding(name, row.subject, row.detail) for row in rows)
    return findings
