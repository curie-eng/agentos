"""collapse blank operator overrides to NULL: agents.model, agents.thinking (#1355)

#1334 taught the API to refuse an empty `thinking`, and #1355 extends that to its
twin `model`. Both fixes guard the WRITE path only, and released images accepted
a blank on both fields, so rows written before this may already hold `''` (or
whitespace). Those rows are the defect frozen into data: the validator will never
see them again, and nothing else repairs them.

A blank override is not a third way to say "no override". `apply_model_env` reads
each as `override if override is not None else config.<field>`, so `''` is not
None and wins the ternary, and then `if value:` is falsy and NO boot key is
emitted -- the agent silently skips the platform default an operator configured
and takes the model's own built-in instead. On the BYO lane that is worse than
cosmetic: `CURIE_MODEL_BASE_URL` and `CURIE_MODEL_API_BACKEND` are config-only
with no override, so they stay set and the SDK dials a custom or OpenRouter
endpoint asking for a built-in Anthropic model id.

NULL is what those rows meant. This converges them onto the one spelling of "no
override" the code actually honors, and it is a no-op on any database that never
took a blank.

Whitespace-only values are collapsed too, by the same predicate the validator
uses: they are worse than empty, since they survive the falsy check and are
forwarded as a garbage id.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"

# The two nullable operator overrides that share `_nullable_override_validator`.
# Adding a third means adding it here too -- that is the point of naming them in
# one list rather than writing the statement twice.
BLANKABLE_OVERRIDES = ("model", "thinking")


def upgrade() -> None:
    for column in BLANKABLE_OVERRIDES:
        # `~ '^[[:space:]]*$'` is the SQL spelling of the validator's
        # `not value.strip()`, and the spelling matters: Postgres `TRIM()` strips
        # SPACES only, so `TRIM(E'\t ') = ''` is false and a tab-indented value
        # would survive a backfill that claimed to match Python. The POSIX class
        # covers space, tab, newline, carriage return, form feed and vertical tab,
        # which is what `str.strip()` removes. Verified on a scratch database
        # against a seeded `E'\t '` row, which the TRIM spelling did NOT collapse.
        #
        # Rows already NULL are untouched, and a real value is only tested, never
        # rewritten -- an empty match set makes this a no-op.
        op.execute(
            f"UPDATE {SCHEMA}.agents SET {column} = NULL "  # noqa: S608 - literal identifiers
            f"WHERE {column} IS NOT NULL AND {column} ~ '^[[:space:]]*$'"
        )


def downgrade() -> None:
    # Deliberately a no-op, and this is NOT laziness: the upgrade merges two
    # states ('' and NULL) into one, so the original cannot be recovered -- there
    # is no record of which NULLs were blanks. Writing `SET '' WHERE NULL` would
    # re-break every row that was correctly NULL all along, which is strictly
    # worse than leaving the repair in place. A downgrade past this revision keeps
    # the corrected data, which the old code reads as "no override" -- the same
    # meaning, and the working one.
    pass
