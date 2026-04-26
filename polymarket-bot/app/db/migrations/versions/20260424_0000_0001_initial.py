"""Initial schema: timescale extension, core tables, hypertable.

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-24 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    op.create_table(
        "trades",
        sa.Column("trade_id", sa.String(128), primary_key=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("market_id", sa.String(128), nullable=False),
        sa.Column("asset_id", sa.String(128), nullable=True),
        sa.Column("maker_address", sa.String(64), nullable=False),
        sa.Column("taker_address", sa.String(64), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("price", sa.Numeric(18, 8), nullable=False),
        sa.Column("size", sa.Numeric(20, 6), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("tx_hash", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_trades_maker_ts", "trades", ["maker_address", "timestamp"])
    op.create_index("ix_trades_market_ts", "trades", ["market_id", "timestamp"])

    # Convert to Timescale hypertable.
    op.execute(
        "SELECT create_hypertable('trades','timestamp', if_not_exists => TRUE, "
        "migrate_data => TRUE)"
    )

    op.create_table(
        "markets",
        sa.Column("condition_id", sa.String(128), primary_key=True),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("slug", sa.String(256), nullable=True),
        sa.Column("token_id_yes", sa.String(128), nullable=True),
        sa.Column("token_id_no", sa.String(128), nullable=True),
        sa.Column("best_ask", sa.Numeric(18, 8)),
        sa.Column("best_bid", sa.Numeric(18, 8)),
        sa.Column("volume_24h", sa.Numeric(20, 6)),
        sa.Column("liquidity_usd", sa.Numeric(20, 6)),
        sa.Column("end_date", sa.DateTime(timezone=True)),
        sa.Column("active", sa.Boolean, server_default=sa.text("true"), nullable=False),
        sa.Column("closed", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("resolved", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("winner", sa.String(16), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "player_stats",
        sa.Column("address", sa.String(64), primary_key=True),
        sa.Column("winrate", sa.Numeric(6, 4), server_default="0"),
        sa.Column("avg_profit_usd", sa.Numeric(20, 6), server_default="0"),
        sa.Column("total_volume_usd", sa.Numeric(20, 6), server_default="0"),
        sa.Column("avg_position_usd", sa.Numeric(20, 6), server_default="0"),
        sa.Column("activity_score", sa.Numeric(6, 4), server_default="0"),
        sa.Column("trades_count", sa.Integer, server_default="0"),
        sa.Column("markets_count", sa.Integer, server_default="0"),
        sa.Column("is_candidate", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_player_stats_candidate", "player_stats", ["is_candidate"], postgresql_where=sa.text("is_candidate = true")
    )

    op.create_table(
        "clusters",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("market_id", sa.String(128), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("score", sa.Numeric(6, 4), nullable=False),
        sa.Column("leader_addrs", sa.ARRAY(sa.String(64)), nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("first_entry_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("avg_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("total_volume_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_clusters_market_id", "clusters", ["market_id"])
    op.create_index("ix_clusters_created", "clusters", ["created_at"])

    op.create_table(
        "signals",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "cluster_id",
            sa.BigInteger,
            sa.ForeignKey("clusters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("market_id", sa.String(128), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("metadata", sa.JSON, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_signals_cluster_id", "signals", ["cluster_id"])

    op.create_table(
        "bot_positions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("market_id", sa.String(128), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("token_id", sa.String(128), nullable=True),
        sa.Column("entry_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("size_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("token_amount", sa.Numeric(20, 6), nullable=False),
        sa.Column(
            "cluster_id",
            sa.BigInteger,
            sa.ForeignKey("clusters.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("leader_addrs", sa.ARRAY(sa.String(64)), nullable=False),
        sa.Column("status", sa.String(16), server_default="open", nullable=False),
        sa.Column("exit_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("pnl_usd", sa.Numeric(20, 6), nullable=True),
        sa.Column("exit_reason", sa.String(64), nullable=True),
        sa.Column("simulation", sa.Boolean, server_default=sa.text("true"), nullable=False),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("market_id", "status", name="uq_open_per_market"),
    )
    op.create_index("ix_bot_positions_market_id", "bot_positions", ["market_id"])

    op.create_table(
        "daily_reports",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("date", sa.DateTime(timezone=True), nullable=False, unique=True),
        sa.Column("trades_count", sa.Integer, server_default="0"),
        sa.Column("wins", sa.Integer, server_default="0"),
        sa.Column("losses", sa.Integer, server_default="0"),
        sa.Column("pnl_usd", sa.Numeric(20, 6), server_default="0"),
        sa.Column("balance_end", sa.Numeric(20, 6), server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("daily_reports")
    op.drop_table("bot_positions")
    op.drop_table("signals")
    op.drop_table("clusters")
    op.drop_table("player_stats")
    op.drop_table("markets")
    op.drop_table("trades")
    # keep timescaledb extension
