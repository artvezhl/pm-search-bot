"""FastAPI endpoints for Grafana / direct operator access."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, FastAPI
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    BotPosition,
    Cluster,
    PMCopySignal,
    PMWallet,
    PMWalletNetwork,
    PlayerStats,
    Trade,
)
from app.db.session import get_async_session


def create_app() -> FastAPI:
    app = FastAPI(title="Polymarket Copy-Trading Bot", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status")
    async def status(session: AsyncSession = Depends(get_async_session)) -> dict[str, Any]:
        s = get_settings()
        r = await session.execute(
            select(func.coalesce(func.sum(BotPosition.pnl_usd), 0)).where(
                BotPosition.status == "closed"
            )
        )
        realised = float(r.scalar() or 0)
        r = await session.execute(
            select(func.count(BotPosition.id)).where(BotPosition.status == "open")
        )
        open_count = int(r.scalar() or 0)
        return {
            "mode": "paper" if s.simulation_mode else "live",
            "starting_balance": s.starting_balance,
            "realised_pnl": realised,
            "balance": s.starting_balance + realised,
            "open_positions": open_count,
            "max_open_positions": s.max_open_positions,
        }

    @app.get("/api/positions")
    async def positions(
        limit: int = 50, session: AsyncSession = Depends(get_async_session)
    ) -> list[dict[str, Any]]:
        rows = (
            await session.execute(
                select(BotPosition)
                .where(BotPosition.status == "open")
                .order_by(desc(BotPosition.opened_at))
                .limit(limit)
            )
        ).scalars().all()
        return [
            {
                "id": p.id,
                "market_id": p.market_id,
                "outcome": p.outcome,
                "entry_price": float(p.entry_price),
                "size_usd": float(p.size_usd),
                "token_amount": float(p.token_amount),
                "leader_addrs": p.leader_addrs,
                "opened_at": p.opened_at.isoformat() if p.opened_at else None,
            }
            for p in rows
        ]

    @app.get("/api/positions/closed")
    async def closed_positions(
        limit: int = 100, session: AsyncSession = Depends(get_async_session)
    ) -> list[dict[str, Any]]:
        rows = (
            await session.execute(
                select(BotPosition)
                .where(BotPosition.status == "closed")
                .order_by(desc(BotPosition.closed_at))
                .limit(limit)
            )
        ).scalars().all()
        return [
            {
                "id": p.id,
                "market_id": p.market_id,
                "outcome": p.outcome,
                "entry_price": float(p.entry_price),
                "exit_price": float(p.exit_price) if p.exit_price is not None else None,
                "size_usd": float(p.size_usd),
                "pnl_usd": float(p.pnl_usd) if p.pnl_usd is not None else None,
                "exit_reason": p.exit_reason,
                "opened_at": p.opened_at.isoformat() if p.opened_at else None,
                "closed_at": p.closed_at.isoformat() if p.closed_at else None,
            }
            for p in rows
        ]

    @app.get("/api/clusters/recent")
    async def recent_clusters(
        hours: int = 24,
        limit: int = 50,
        session: AsyncSession = Depends(get_async_session),
    ) -> list[dict[str, Any]]:
        since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
        rows = (
            await session.execute(
                select(Cluster)
                .where(Cluster.created_at >= since)
                .order_by(desc(Cluster.score))
                .limit(limit)
            )
        ).scalars().all()
        return [
            {
                "id": c.id,
                "market_id": c.market_id,
                "outcome": c.outcome,
                "score": float(c.score),
                "size": c.size,
                "total_volume_usd": float(c.total_volume_usd),
                "avg_price": float(c.avg_price),
                "first_entry_ts": c.first_entry_ts.isoformat() if c.first_entry_ts else None,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in rows
        ]

    @app.get("/api/stats")
    async def stats(session: AsyncSession = Depends(get_async_session)) -> dict[str, Any]:
        closed = (
            await session.execute(
                select(BotPosition).where(BotPosition.status == "closed")
            )
        ).scalars().all()
        total = len(closed)
        wins = sum(1 for p in closed if float(p.pnl_usd or 0) > 0)
        winrate = wins / total if total else 0.0
        pnl = sum(float(p.pnl_usd or 0) for p in closed)
        s = get_settings()
        return {
            "total_trades": total,
            "wins": wins,
            "losses": total - wins,
            "winrate": winrate,
            "pnl_total": pnl,
            "balance": s.starting_balance + pnl,
        }

    @app.get("/api/players/top")
    async def top_players(
        limit: int = 50, session: AsyncSession = Depends(get_async_session)
    ) -> list[dict[str, Any]]:
        rows = (
            await session.execute(
                select(PlayerStats)
                .where(PlayerStats.is_candidate.is_(True))
                .order_by(desc(PlayerStats.activity_score), desc(PlayerStats.total_volume_usd))
                .limit(limit)
            )
        ).scalars().all()
        return [
            {
                "address": p.address,
                "winrate": float(p.winrate),
                "trades_count": p.trades_count,
                "markets_count": p.markets_count,
                "total_volume_usd": float(p.total_volume_usd),
                "avg_position_usd": float(p.avg_position_usd),
                "activity_score": float(p.activity_score),
            }
            for p in rows
        ]

    @app.get("/api/trades/latest")
    async def latest_trades(
        limit: int = 100, session: AsyncSession = Depends(get_async_session)
    ) -> list[dict[str, Any]]:
        rows = (
            await session.execute(
                select(Trade).order_by(desc(Trade.timestamp)).limit(limit)
            )
        ).scalars().all()
        return [
            {
                "trade_id": t.trade_id,
                "market_id": t.market_id,
                "maker_address": t.maker_address,
                "outcome": t.outcome,
                "price": float(t.price),
                "size": float(t.size),
                "side": t.side,
                "timestamp": t.timestamp.isoformat(),
            }
            for t in rows
        ]

    @app.get("/api/pm/wallets/top")
    async def pm_top_wallets(
        limit: int = 50, session: AsyncSession = Depends(get_async_session)
    ) -> list[dict[str, Any]]:
        rows = (
            await session.execute(
                select(PMWallet)
                .where(PMWallet.is_smart_wallet.is_(True))
                .order_by(desc(PMWallet.smart_score), desc(PMWallet.total_volume_usdc))
                .limit(limit)
            )
        ).scalars().all()
        return [
            {
                "address": r.address,
                "smart_score": float(r.smart_score or 0),
                "win_rate": float(r.win_rate or 0),
                "resolved_markets": r.resolved_markets,
                "total_volume_usdc": float(r.total_volume_usdc or 0),
            }
            for r in rows
        ]

    @app.get("/api/pm/signals")
    async def pm_signals(
        limit: int = 100, session: AsyncSession = Depends(get_async_session)
    ) -> list[dict[str, Any]]:
        rows = (
            await session.execute(
                select(PMCopySignal).order_by(desc(PMCopySignal.created_at)).limit(limit)
            )
        ).scalars().all()
        return [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat(),
                "trigger_wallet": r.trigger_wallet,
                "condition_id": r.condition_id,
                "token_outcome": r.token_outcome,
                "status": r.status,
                "suggested_amount": float(r.suggested_amount or 0),
                "execution_amount": float(r.execution_amount or 0),
                "pnl_usdc": float(r.pnl_usdc or 0),
            }
            for r in rows
        ]

    @app.get("/api/pm/networks")
    async def pm_networks(
        limit: int = 100, session: AsyncSession = Depends(get_async_session)
    ) -> list[dict[str, Any]]:
        rows = (
            await session.execute(
                select(PMWalletNetwork)
                .order_by(desc(PMWalletNetwork.correlation_score), desc(PMWalletNetwork.co_trade_count))
                .limit(limit)
            )
        ).scalars().all()
        return [
            {
                "wallet_a": r.wallet_a,
                "wallet_b": r.wallet_b,
                "co_trade_count": r.co_trade_count,
                "correlation_score": float(r.correlation_score or 0),
                "last_co_trade_at": r.last_co_trade_at.isoformat() if r.last_co_trade_at else None,
            }
            for r in rows
        ]

    return app


app = create_app()
