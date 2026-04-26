"""Add pm_* analytics tables.

Revision ID: 0002_pm_analytic_module
Revises: 0001_initial
Create Date: 2026-04-25 13:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_pm_analytic_module"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pm_markets",
        sa.Column("condition_id", sa.String(length=66), primary_key=True),
        sa.Column("event_market_name", sa.Text(), nullable=True),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("token_outcome", sa.String(length=10), nullable=True),
        sa.Column("asset_id", sa.Numeric(), nullable=True),
        sa.Column("neg_risk", sa.Boolean(), nullable=True),
        sa.Column("polymarket_link", sa.Text(), nullable=True),
        sa.Column("resolved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("resolution_value", sa.Numeric(10, 4), nullable=True),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "pm_trades",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tx_hash", sa.String(length=66), nullable=False),
        sa.Column("block_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("condition_id", sa.String(length=66), sa.ForeignKey("pm_markets.condition_id"), nullable=True),
        sa.Column("token_outcome", sa.String(length=10), nullable=True),
        sa.Column("maker_address", sa.String(length=42), nullable=False),
        sa.Column("taker_address", sa.String(length=42), nullable=True),
        sa.Column("price", sa.Numeric(10, 6), nullable=False),
        sa.Column("amount_usdc", sa.Numeric(18, 6), nullable=False),
        sa.Column("shares", sa.Numeric(18, 6), nullable=True),
        sa.Column("action", sa.String(length=20), nullable=True),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tx_hash", "condition_id", "maker_address", name="uq_pm_trades_trade"),
    )
    op.create_index("idx_pm_trades_maker", "pm_trades", ["maker_address", "block_time"])
    op.create_index("idx_pm_trades_market", "pm_trades", ["condition_id", "block_time"])
    op.create_index("idx_pm_trades_time", "pm_trades", ["block_time"])

    op.create_table(
        "pm_wallets",
        sa.Column("address", sa.String(length=42), primary_key=True),
        sa.Column("total_markets", sa.Integer(), server_default="0", nullable=False),
        sa.Column("resolved_markets", sa.Integer(), server_default="0", nullable=False),
        sa.Column("winning_markets", sa.Integer(), server_default="0", nullable=False),
        sa.Column("win_rate", sa.Numeric(5, 4), nullable=True),
        sa.Column("total_volume_usdc", sa.Numeric(18, 2), server_default="0", nullable=False),
        sa.Column("total_pnl_usdc", sa.Numeric(18, 2), server_default="0", nullable=False),
        sa.Column("avg_roi", sa.Numeric(8, 4), nullable=True),
        sa.Column("avg_entry_price", sa.Numeric(6, 4), nullable=True),
        sa.Column("avg_entry_days_before_close", sa.Numeric(8, 2), nullable=True),
        sa.Column("first_trade_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_trade_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_last_30d", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("smart_score", sa.Numeric(6, 4), nullable=True),
        sa.Column("is_smart_wallet", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_blacklisted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("stats_updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "idx_pm_wallets_score",
        "pm_wallets",
        ["smart_score"],
        postgresql_where=sa.text("is_smart_wallet = true"),
    )
    op.create_index("idx_pm_wallets_active", "pm_wallets", ["last_trade_at"])

    op.create_table(
        "pm_wallet_networks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("wallet_a", sa.String(length=42), nullable=False),
        sa.Column("wallet_b", sa.String(length=42), nullable=False),
        sa.Column("co_trade_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("correlation_score", sa.Numeric(5, 4), nullable=True),
        sa.Column("last_co_trade_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("wallet_a", "wallet_b", name="uq_pm_wallet_pair"),
    )

    op.create_table(
        "pm_copy_signals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("trigger_wallet", sa.String(length=42), nullable=False),
        sa.Column("trigger_tx_hash", sa.String(length=66), nullable=True),
        sa.Column("trigger_trade_id", sa.BigInteger(), sa.ForeignKey("pm_trades.id"), nullable=True),
        sa.Column("condition_id", sa.String(length=66), sa.ForeignKey("pm_markets.condition_id"), nullable=True),
        sa.Column("token_outcome", sa.String(length=10), nullable=True),
        sa.Column("action", sa.String(length=10), nullable=True),
        sa.Column("suggested_price", sa.Numeric(10, 6), nullable=True),
        sa.Column("suggested_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="PENDING", nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_tx_hash", sa.String(length=66), nullable=True),
        sa.Column("execution_price", sa.Numeric(10, 6), nullable=True),
        sa.Column("execution_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("pnl_usdc", sa.Numeric(18, 2), nullable=True),
        sa.Column("pnl_pct", sa.Numeric(8, 4), nullable=True),
    )
    op.create_index("idx_pm_copy_signals_status", "pm_copy_signals", ["status", "created_at"])
    op.create_index("idx_pm_copy_signals_wallet", "pm_copy_signals", ["trigger_wallet", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_pm_copy_signals_wallet", table_name="pm_copy_signals")
    op.drop_index("idx_pm_copy_signals_status", table_name="pm_copy_signals")
    op.drop_table("pm_copy_signals")
    op.drop_table("pm_wallet_networks")
    op.drop_index("idx_pm_wallets_active", table_name="pm_wallets")
    op.drop_index("idx_pm_wallets_score", table_name="pm_wallets")
    op.drop_table("pm_wallets")
    op.drop_index("idx_pm_trades_time", table_name="pm_trades")
    op.drop_index("idx_pm_trades_market", table_name="pm_trades")
    op.drop_index("idx_pm_trades_maker", table_name="pm_trades")
    op.drop_table("pm_trades")
    op.drop_table("pm_markets")
