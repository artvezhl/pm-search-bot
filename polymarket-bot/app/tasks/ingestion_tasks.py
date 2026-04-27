"""Celery tasks for Layer 1."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from redis.asyncio import Redis

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.db.models import IngestionRun
from app.execution.trade_executor import TradeExecutor
from app.ingestion.dune_history_loader import DuneHistoryLoader
from app.ingestion.history_loader import HistoryLoader
from app.ingestion.ingestion_service import IngestionService
from app.ingestion.market_sync import MarketSync
from app.ingestion.onchain_backfill import OnchainTradeBackfiller
from app.ingestion.trade_poller import TradePoller
from app.logger import logger
from app.tasks.celery_app import celery_app


def _run(coro) -> None:  # type: ignore[no-untyped-def]
    """Synchronously run a coroutine inside a Celery worker thread."""
    try:
        asyncio.run(coro)
    except Exception as e:  # noqa: BLE001
        logger.exception("Celery task failed: {}", e)
        raise


@celery_app.task(name="app.tasks.ingestion_tasks.refresh_markets_and_trades")
def refresh_markets_and_trades() -> str:
    service = IngestionService()
    _run(service.refresh_markets_and_trades())
    return "ok"


@celery_app.task(name="app.tasks.ingestion_tasks.pm_market_sync")
def pm_market_sync() -> int:
    return asyncio.run(MarketSync().refresh())


@celery_app.task(name="app.tasks.ingestion_tasks.pm_market_sync_traded_metadata")
def pm_market_sync_traded_metadata() -> int:
    return asyncio.run(MarketSync().refresh_traded_metadata())


@celery_app.task(name="app.tasks.ingestion_tasks.pm_trade_poller")
def pm_trade_poller() -> int:
    return asyncio.run(TradePoller().poll_once())


@celery_app.task(name="app.tasks.ingestion_tasks.pm_execute_signal_queue")
def pm_execute_signal_queue() -> int:
    async def _run_once() -> int:
        redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
        item = await redis.rpop("polymarket:signals")
        if not item:
            return 0
        payload = json.loads(item)
        signal_id = int(payload.get("signal_id", 0))
        if not signal_id:
            return 0
        ok = await TradeExecutor().execute_pm_signal(signal_id)
        return 1 if ok else 0

    return asyncio.run(_run_once())


@celery_app.task(name="app.tasks.ingestion_tasks.pm_history_load")
def pm_history_load(
    query_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    return asyncio.run(HistoryLoader().run(query_id, date_from=date_from, date_to=date_to))


@celery_app.task(name="app.tasks.ingestion_tasks.pm_history_load_dune")
def pm_history_load_dune(
    query_id: int,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    return asyncio.run(
        DuneHistoryLoader().run(
            query_id=query_id,
            date_from=date_from,
            date_to=date_to,
        )
    )


@celery_app.task(name="app.tasks.ingestion_tasks.pm_history_load_daily_range")
def pm_history_load_daily_range(
    query_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
) -> int:
    async def _tracked_run() -> int:
        async with AsyncSessionLocal() as session:
            run = IngestionRun(
                task_name="app.tasks.ingestion_tasks.pm_history_load_daily_range",
                status="running",
                date_from=datetime.fromisoformat(date_from) if date_from else None,
                date_to=datetime.fromisoformat(date_to) if date_to else None,
                inserted_rows=0,
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)

            try:
                inserted = await HistoryLoader().run_daily_range(
                    query_id, date_from=date_from, date_to=date_to
                )
                run.status = "success"
                run.inserted_rows = inserted
                run.finished_at = datetime.now(timezone.utc)
                await session.commit()
                return inserted
            except Exception as e:  # noqa: BLE001
                run.status = "failed"
                run.error_message = str(e)[:2000]
                run.finished_at = datetime.now(timezone.utc)
                await session.commit()
                raise

    return asyncio.run(_tracked_run())


@celery_app.task(name="app.tasks.ingestion_tasks.backfill_onchain_trades")
def backfill_onchain_trades() -> int:
    settings = get_settings()
    if not settings.onchain_backfill_enabled:
        return 0
    rpc_url = settings.alchemy_polygon_url or settings.polygon_rpc_url
    if not rpc_url:
        logger.warning(
            "ONCHAIN_BACKFILL_ENABLED=true but both ALCHEMY_POLYGON_URL and POLYGON_RPC_URL are empty"
        )
        return 0
    backfiller = OnchainTradeBackfiller(
        rpc_url=rpc_url,
        lookback_days=settings.onchain_backfill_days,
        chunk_size=settings.onchain_backfill_chunk_size,
    )
    return asyncio.run(backfiller.run())
