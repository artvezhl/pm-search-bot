"""Add ingestion_runs table for task observability.

Revision ID: 0003_ingestion_runs
Revises: 0002_pm_analytic_module
Create Date: 2026-04-27 10:58:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_ingestion_runs"
down_revision: str | None = "0002_pm_analytic_module"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("task_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("date_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("date_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("inserted_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_ingestion_runs_status", "ingestion_runs", ["status"])
    op.create_index("idx_ingestion_runs_task_started", "ingestion_runs", ["task_name", "started_at"])


def downgrade() -> None:
    op.drop_index("idx_ingestion_runs_task_started", table_name="ingestion_runs")
    op.drop_index("idx_ingestion_runs_status", table_name="ingestion_runs")
    op.drop_table("ingestion_runs")
