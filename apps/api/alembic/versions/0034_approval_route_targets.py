"""split approval notification from the resolution target (#1460)

The approval route's old ``channel`` field fused where humans were notified
with where a verified identity could act. Existing durable JSONB rows are
rewritten here so the application and worker can reject that retired shape at
every runtime boundary without silently unbinding deployed routes.

Downgrade is loss-aware. The legacy shape can represent only one Slack
resolution channel and no second notification target, so any richer row is
named and refused before the first write.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"
TABLE = "agents"
SLACK = "slack"


def _lock_agents() -> None:
    """Freeze approval-route writers for this transaction's inspect/rewrite."""

    # The preflight and rewrite are one decision. Without this table lock, a
    # concurrent agent PATCH can insert a malformed legacy/split binding after
    # inspection and before the UPDATE, defeating the migration's all-or-none
    # claim. SHARE ROW EXCLUSIVE blocks INSERT/UPDATE/DELETE while preserving
    # ordinary reads, and PostgreSQL holds it until Alembic commits or rolls back.
    op.get_bind().execute(
        sa.text(f"LOCK TABLE {SCHEMA}.{TABLE} IN SHARE ROW EXCLUSIVE MODE")
    )


def _rows_with_routes() -> list[tuple[str, Any]]:
    return [
        (row.name, row.approval_routes)
        for row in op.get_bind()
        .execute(
            sa.text(
                f"""
                SELECT name, approval_routes
                FROM {SCHEMA}.{TABLE}
                WHERE approval_routes IS NOT NULL
                  AND approval_routes <> 'null'::jsonb
                ORDER BY name
                """
            )
        )
        .all()
    ]


def _legacy_violations() -> list[str]:
    violations: list[str] = []
    for agent, routes in _rows_with_routes():
        if not isinstance(routes, Mapping):
            violations.append(f"{agent} (<approval_routes>: expected an object)")
            continue
        for route, binding in routes.items():
            route_label = str(route)
            if not isinstance(route, str) or not route.strip():
                violations.append(f"{agent} ({route_label!r}: invalid route name)")
                continue
            if not isinstance(binding, Mapping):
                violations.append(f"{agent} ({route}: binding is not an object)")
                continue
            unknown = sorted(set(binding) - {"channel", "approvers"})
            channel = binding.get("channel")
            if unknown:
                violations.append(f"{agent} ({route}: unknown keys {unknown})")
            elif not isinstance(channel, str) or not channel.strip():
                violations.append(f"{agent} ({route}: channel is not a non-empty string)")
            elif "approvers" in binding and not isinstance(binding["approvers"], Mapping):
                violations.append(f"{agent} ({route}: approvers is not an object)")
    return violations


def upgrade() -> None:
    _lock_agents()
    violations = _legacy_violations()
    if violations:
        raise RuntimeError(
            "cannot split approval resolution from notification (#1460): these "
            "legacy bindings are malformed and no row was rewritten -- "
            + "; ".join(violations)
            + ". Repair or remove the named routes, then re-run this migration."
        )

    # One statement after the complete preflight: every populated map changes
    # together, route names and approvers are retained byte-for-byte as JSONB,
    # and NULL/empty maps stay NULL/empty.
    op.get_bind().execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.{TABLE} AS a
            SET approval_routes = (
                SELECT jsonb_object_agg(
                    route.key,
                    (route.value - 'channel') || jsonb_build_object(
                        'resolution',
                        jsonb_build_object(
                            'kind', CAST(:slack AS text),
                            'address', route.value -> 'channel'
                        )
                    )
                )
                FROM jsonb_each(a.approval_routes) AS route
            )
            WHERE a.approval_routes IS NOT NULL
              AND a.approval_routes <> 'null'::jsonb
              AND a.approval_routes <> '{{}}'::jsonb
            """
        ),
        {"slack": SLACK},
    )


def _split_violations() -> list[str]:
    violations: list[str] = []
    for agent, routes in _rows_with_routes():
        if not isinstance(routes, Mapping):
            violations.append(f"{agent} (<approval_routes>: expected an object)")
            continue
        for route, binding in routes.items():
            route_label = str(route)
            if not isinstance(route, str) or not route.strip():
                violations.append(f"{agent} ({route_label!r}: invalid route name)")
                continue
            if not isinstance(binding, Mapping):
                violations.append(f"{agent} ({route}: binding is not an object)")
                continue
            if "notification" in binding:
                violations.append(f"{agent} ({route}: has a notification target)")
                continue
            unknown = sorted(set(binding) - {"resolution", "approvers"})
            resolution = binding.get("resolution")
            if unknown:
                violations.append(f"{agent} ({route}: unknown keys {unknown})")
            elif not isinstance(resolution, Mapping):
                violations.append(f"{agent} ({route}: resolution is not an object)")
            elif set(resolution) != {"kind", "address"}:
                violations.append(f"{agent} ({route}: malformed resolution target)")
            elif resolution.get("kind") != SLACK:
                violations.append(
                    f"{agent} ({route}: resolution kind {resolution.get('kind')!r} is not slack)"
                )
            elif (
                not isinstance(resolution.get("address"), str) or not resolution["address"].strip()
            ):
                violations.append(f"{agent} ({route}: resolution address is invalid)")
            elif "approvers" in binding and not isinstance(binding["approvers"], Mapping):
                violations.append(f"{agent} ({route}: approvers is not an object)")
    return violations


def downgrade() -> None:
    _lock_agents()
    violations = _split_violations()
    if violations:
        raise RuntimeError(
            "cannot restore the fused approval route channel (#1460): these "
            "bindings cannot be represented without losing a notification or "
            "relabeling a resolver, and no row was rewritten -- "
            + "; ".join(violations)
            + ". Remove the notification targets or restore Slack-only "
            "resolutions on the named routes, then re-run this downgrade."
        )

    op.get_bind().execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.{TABLE} AS a
            SET approval_routes = (
                SELECT jsonb_object_agg(
                    route.key,
                    (route.value - 'resolution') || jsonb_build_object(
                        'channel', route.value #> '{{resolution,address}}'
                    )
                )
                FROM jsonb_each(a.approval_routes) AS route
            )
            WHERE a.approval_routes IS NOT NULL
              AND a.approval_routes <> 'null'::jsonb
              AND a.approval_routes <> '{{}}'::jsonb
            """
        )
    )
