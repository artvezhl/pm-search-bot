"""Celery task that applies exit rules to open positions."""

from __future__ import annotations

import asyncio

from app.execution.trade_executor import TradeExecutor
from app.logger import logger
from app.signal.exit_watcher import ExitWatcher
from app.tasks.celery_app import celery_app


async def _scan_exits_async() -> int:
    watcher = ExitWatcher()
    executor = TradeExecutor()
    intents = await watcher.scan()
    for intent in intents:
        try:
            await executor.handle_exit(intent)
        except Exception as e:  # noqa: BLE001
            logger.exception("Exit execution failed: {}", e)
    return len(intents)


@celery_app.task(name="app.tasks.exit_tasks.scan_exits")
def scan_exits() -> int:
    n: int = asyncio.run(_scan_exits_async())
    return n
