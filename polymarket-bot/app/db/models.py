"""SQLAlchemy ORM models backing the MVP.

Note: `trades` is a TimescaleDB hypertable (created in the initial migration).
Because Timescale only allows composite PKs that include the time column,
the `trades.id` primary key is (trade_id, timestamp).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text as sa_text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"

    # Composite PK so TimescaleDB can create the hypertable on `timestamp`.
    trade_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)

    market_id: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    maker_address: Mapped[str] = mapped_column(String(64), nullable=False)
    taker_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    outcome: Mapped[str] = mapped_column(String(16), nullable=False)  # "YES" / "NO"
    price: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    size: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)  # USDC notional
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # "BUY" / "SELL"

    tx_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_trades_maker_ts", "maker_address", "timestamp"),
        Index("ix_trades_market_ts", "market_id", "timestamp"),
    )


class Market(Base):
    __tablename__ = "markets"

    condition_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    slug: Mapped[str | None] = mapped_column(String(256), nullable=True)

    token_id_yes: Mapped[str | None] = mapped_column(String(128), nullable=True)
    token_id_no: Mapped[str | None] = mapped_column(String(128), nullable=True)

    best_ask: Mapped[float | None] = mapped_column(Numeric(18, 8))
    best_bid: Mapped[float | None] = mapped_column(Numeric(18, 8))
    volume_24h: Mapped[float | None] = mapped_column(Numeric(20, 6))
    liquidity_usd: Mapped[float | None] = mapped_column(Numeric(20, 6))

    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    closed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    winner: Mapped[str | None] = mapped_column(String(16), nullable=True)  # "YES"/"NO"

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PlayerStats(Base):
    __tablename__ = "player_stats"

    address: Mapped[str] = mapped_column(String(64), primary_key=True)
    winrate: Mapped[float] = mapped_column(Numeric(6, 4), default=0)
    avg_profit_usd: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    total_volume_usd: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    avg_position_usd: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    activity_score: Mapped[float] = mapped_column(Numeric(6, 4), default=0)
    trades_count: Mapped[int] = mapped_column(Integer, default=0)
    markets_count: Mapped[int] = mapped_column(Integer, default=0)
    is_candidate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    leader_addrs: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    first_entry_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    avg_price: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    total_volume_usd: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    signals: Mapped[list["Signal"]] = relationship(back_populates="cluster")

    __table_args__ = (Index("ix_clusters_created", "created_at"),)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cluster_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    market_id: Mapped[str] = mapped_column(String(128), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)  # ENTER / SKIP_*
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    cluster: Mapped[Cluster] = relationship(back_populates="signals")


class BotPosition(Base):
    __tablename__ = "bot_positions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    market_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    token_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    entry_price: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    size_usd: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    token_amount: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)

    cluster_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("clusters.id", ondelete="SET NULL"), nullable=True
    )
    leader_addrs: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)

    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)  # open/closed
    exit_price: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    pnl_usd: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    simulation: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("market_id", "status", name="uq_open_per_market"),)


class DailyReport(Base):
    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, unique=True)
    trades_count: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    pnl_usd: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    balance_end: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PMMarket(Base):
    __tablename__ = "pm_markets"

    condition_id: Mapped[str] = mapped_column(String(66), primary_key=True)
    event_market_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_outcome: Mapped[str | None] = mapped_column(String(10), nullable=True)
    asset_id: Mapped[int | None] = mapped_column(Numeric, nullable=True)
    neg_risk: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    polymarket_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolution_value: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    close_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PMTrade(Base):
    __tablename__ = "pm_trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tx_hash: Mapped[str] = mapped_column(String(66), nullable=False)
    block_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    condition_id: Mapped[str | None] = mapped_column(
        String(66), ForeignKey("pm_markets.condition_id"), nullable=True
    )
    token_outcome: Mapped[str | None] = mapped_column(String(10), nullable=True)
    maker_address: Mapped[str] = mapped_column(String(42), nullable=False)
    taker_address: Mapped[str | None] = mapped_column(String(42), nullable=True)
    price: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)
    amount_usdc: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    shares: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    action: Mapped[str | None] = mapped_column(String(20), nullable=True)
    raw_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("tx_hash", "condition_id", "maker_address", name="uq_pm_trades_trade"),
        Index("idx_pm_trades_maker", "maker_address", "block_time"),
        Index("idx_pm_trades_market", "condition_id", "block_time"),
        Index("idx_pm_trades_time", "block_time"),
    )


class PMWallet(Base):
    __tablename__ = "pm_wallets"

    address: Mapped[str] = mapped_column(String(42), primary_key=True)
    total_markets: Mapped[int] = mapped_column(Integer, default=0)
    resolved_markets: Mapped[int] = mapped_column(Integer, default=0)
    winning_markets: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    total_volume_usdc: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    total_pnl_usdc: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    avg_roi: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    avg_entry_price: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    avg_entry_days_before_close: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    first_trade_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_trade_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active_last_30d: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    smart_score: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    is_smart_wallet: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_blacklisted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stats_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "idx_pm_wallets_score",
            "smart_score",
            postgresql_where=sa_text("is_smart_wallet = true"),
        ),
        Index("idx_pm_wallets_active", "last_trade_at"),
    )


class PMWalletNetwork(Base):
    __tablename__ = "pm_wallet_networks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    wallet_a: Mapped[str] = mapped_column(String(42), nullable=False)
    wallet_b: Mapped[str] = mapped_column(String(42), nullable=False)
    co_trade_count: Mapped[int] = mapped_column(Integer, default=0)
    correlation_score: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    last_co_trade_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("wallet_a", "wallet_b", name="uq_pm_wallet_pair"),)


class PMCopySignal(Base):
    __tablename__ = "pm_copy_signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    trigger_wallet: Mapped[str] = mapped_column(String(42), nullable=False)
    trigger_tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    trigger_trade_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("pm_trades.id"), nullable=True
    )
    condition_id: Mapped[str | None] = mapped_column(
        String(66), ForeignKey("pm_markets.condition_id"), nullable=True
    )
    token_outcome: Mapped[str | None] = mapped_column(String(10), nullable=True)
    action: Mapped[str | None] = mapped_column(String(10), nullable=True)
    suggested_price: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    suggested_amount: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    execution_tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    execution_price: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    execution_amount: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    pnl_usdc: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)

    __table_args__ = (
        Index("idx_pm_copy_signals_status", "status", "created_at"),
        Index("idx_pm_copy_signals_wallet", "trigger_wallet", "created_at"),
    )


__all__ = [
    "Base",
    "Trade",
    "Market",
    "PlayerStats",
    "Cluster",
    "Signal",
    "BotPosition",
    "DailyReport",
    "PMMarket",
    "PMTrade",
    "PMWallet",
    "PMWalletNetwork",
    "PMCopySignal",
]
