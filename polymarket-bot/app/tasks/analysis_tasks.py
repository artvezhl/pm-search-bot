"""Celery tasks for Layer 2 + signal pipeline + daily report."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.analysis.cluster_detector import ClusterDetector
from app.analysis.network_detector import NetworkDetector
from app.analysis.player_tracker import PlayerTracker
from app.analysis.wallet_analyzer import WalletAnalyzer
from app.db import AsyncSessionLocal
from app.db.models import BotPosition, DailyReport, PMTrade
from app.execution.trade_executor import TradeExecutor
from app.logger import logger
from app.notifications.notifier import get_notifier
from app.signal.signal_engine import SignalEngine
from app.tasks.celery_app import celery_app


def _run(coro) -> None:  # type: ignore[no-untyped-def]
    try:
        asyncio.run(coro)
    except Exception as e:  # noqa: BLE001
        logger.exception("Celery task failed: {}", e)
        raise


async def _update_player_stats_async() -> int:
    return await PlayerTracker().refresh()


async def _update_pm_wallets_async() -> int:
    return await WalletAnalyzer().refresh()


async def _update_pm_networks_async() -> int:
    return await NetworkDetector().refresh()


async def _detect_clusters_and_signal_async() -> None:
    detector = ClusterDetector()
    engine = SignalEngine()
    executor = TradeExecutor()

    clusters = await detector.detect()
    for c in clusters:
        decision = await engine.evaluate(c)
        if decision.decision == "ENTER":
            await executor.handle_entry(decision)


async def _send_daily_report_async() -> None:
    since = datetime.now(tz=timezone.utc) - timedelta(hours=24)

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(func.count(), func.coalesce(func.sum(BotPosition.pnl_usd), 0)).where(
                BotPosition.status == "closed", BotPosition.closed_at >= since
            )
        )
        count, pnl = r.one()
        count = int(count or 0)
        pnl_f = float(pnl or 0)

        r = await session.execute(
            select(func.count()).where(
                BotPosition.status == "closed",
                BotPosition.closed_at >= since,
                BotPosition.pnl_usd > 0,
            )
        )
        wins = int(r.scalar() or 0)
        losses = count - wins

        total_r = await session.execute(
            select(func.coalesce(func.sum(BotPosition.pnl_usd), 0)).where(
                BotPosition.status == "closed"
            )
        )
        realised_total = float(total_r.scalar() or 0)

        from app.config import get_settings

        balance = get_settings().starting_balance + realised_total
        stmt = pg_insert(DailyReport).values(
            date=datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
            trades_count=count,
            wins=wins,
            losses=losses,
            pnl_usd=Decimal(str(round(pnl_f, 6))),
            balance_end=Decimal(str(round(balance, 6))),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["date"],
            set_={
                "trades_count": count,
                "wins": wins,
                "losses": losses,
                "pnl_usd": Decimal(str(round(pnl_f, 6))),
                "balance_end": Decimal(str(round(balance, 6))),
            },
        )
        await session.execute(stmt)
        await session.commit()

    msg = (
        "📊 *Daily report*\n"
        f"Trades (24h): {count}  |  wins: {wins}  losses: {losses}\n"
        f"PnL (24h): ${pnl_f:,.2f}\n"
        f"Balance: ${balance:,.2f}"
    )
    await get_notifier().send(msg)


async def _cleanup_old_pm_trades_async() -> int:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=180)
    async with AsyncSessionLocal() as session:
        result = await session.execute(delete(PMTrade).where(PMTrade.block_time < cutoff))
        await session.commit()
    return int(result.rowcount or 0)


@celery_app.task(name="app.tasks.analysis_tasks.update_player_stats")
def update_player_stats() -> int:
    n: int = asyncio.run(_update_player_stats_async())
    return n


@celery_app.task(name="app.tasks.analysis_tasks.update_pm_wallets")
def update_pm_wallets() -> int:
    return asyncio.run(_update_pm_wallets_async())


@celery_app.task(name="app.tasks.analysis_tasks.update_pm_networks")
def update_pm_networks() -> int:
    return asyncio.run(_update_pm_networks_async())


@celery_app.task(name="app.tasks.analysis_tasks.detect_clusters_and_signal")
def detect_clusters_and_signal() -> str:
    _run(_detect_clusters_and_signal_async())
    return "ok"


@celery_app.task(name="app.tasks.analysis_tasks.send_daily_report")
def send_daily_report() -> str:
    _run(_send_daily_report_async())
    return "ok"


@celery_app.task(name="app.tasks.analysis_tasks.cleanup_old_pm_trades")
def cleanup_old_pm_trades() -> int:
    return asyncio.run(_cleanup_old_pm_trades_async())
