"""Managed repository workspaces and approval-gated publications.

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column("workspace_repo", sa.String(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "approvals",
        sa.Column(
            "purpose",
            sa.String(),
            nullable=False,
            server_default="session",
        ),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "approvals_purpose_ck",
        "approvals",
        "purpose IN ('session', 'publication')",
        schema=SCHEMA,
    )

    op.create_table(
        "publications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deployment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repo_full_name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("base_sha", sa.String(), nullable=False),
        sa.Column("patch_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("changed_paths", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("reply_kind", sa.String(), nullable=False),
        sa.Column("reply_channel", sa.String(), nullable=False),
        sa.Column("reply_placeholder", sa.String(), nullable=True),
        sa.Column("reply_endpoint", sa.String(), nullable=True),
        sa.Column("reply_adapter", sa.String(), nullable=True),
        sa.Column("lease_owner", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("result_url", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result_reported_at", sa.DateTime(), nullable=True),
        sa.Column(
            "result_delivery_attempts", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("result_delivery_error", sa.Text(), nullable=True),
        sa.Column("result_delivery_dead_lettered_at", sa.DateTime(), nullable=True),
        sa.Column("reconcile_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reconcile_dead_lettered_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("terminal_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'expired', "
            "'launching', 'running', 'succeeded', 'failed')",
            name="publications_status_ck",
        ),
        sa.CheckConstraint("version >= 1", name="publications_version_ck"),
        sa.CheckConstraint(
            "result_delivery_attempts >= 0",
            name="publications_result_delivery_attempts_ck",
        ),
        sa.CheckConstraint(
            "reconcile_attempts >= 0", name="publications_reconcile_attempts_ck"
        ),
        sa.CheckConstraint(
            "octet_length(patch_bytes) <= 900000",
            name="publications_patch_size_ck",
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"], [f"{SCHEMA}.approvals.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"], [f"{SCHEMA}.deployments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("approval_id", name="publications_approval_id_key"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_publications_status_lease",
        "publications",
        ["status", "lease_expires_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_publications_deployment_id",
        "publications",
        ["deployment_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_publications_result_delivery",
        "publications",
        ["result_reported_at", "result_delivery_dead_lettered_at", "lease_expires_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "credential_redemption_audit_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("deployment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("publication_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("repo_full_name", sa.String(), nullable=True),
        sa.Column("detail", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "purpose IN ('workspace_clone', 'publication_push')",
            name="credential_redemption_purpose_ck",
        ),
        sa.CheckConstraint(
            "outcome IN ('issued', 'refused')",
            name="credential_redemption_outcome_ck",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"], [f"{SCHEMA}.deployments.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"], [f"{SCHEMA}.publications.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_credential_redemption_deployment",
        "credential_redemption_audit_entries",
        ["deployment_id", "created_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_credential_redemption_publication",
        "credential_redemption_audit_entries",
        ["publication_id", "created_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("credential_redemption_audit_entries", schema=SCHEMA)
    op.drop_table("publications", schema=SCHEMA)
    op.drop_constraint(
        "approvals_purpose_ck", "approvals", schema=SCHEMA, type_="check"
    )
    op.drop_column("approvals", "purpose", schema=SCHEMA)
    op.drop_column("deployments", "workspace_repo", schema=SCHEMA)
