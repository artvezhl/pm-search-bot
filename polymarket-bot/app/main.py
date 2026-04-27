"""Process entrypoint for the `app` container.

Responsibilities (all in the same event loop):

    * Telegram bot polling
    * CLOB ingestion service (REST poll + WebSocket stream)
    * Polygon on-chain listener (resolution events)
    * FastAPI HTTP server (dashboard)

Celery worker + beat run in separate containers and handle scheduled jobs
(player_tracker, cluster detection, exit watcher, daily report).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

import uvicorn

from app.api.dashboard import app as fastapi_app
from app.config import get_settings
from app.ingestion.ingestion_service import IngestionService
from app.ingestion.polygon_listener import PolygonListener
from app.logger import logger
from app.notifications.telegram_bot import run_bot


async def _on_resolution(event: dict) -> None:  # type: ignore[type-arg]
    """Polygon resolution event handler — persists winner on the market row."""
    from sqlalchemy import update

    from app.db import AsyncSessionLocal
    from app.db.models import Market

    payouts = event.get("payout_numerators") or []
    # Polymarket binary markets: YES outcome index 0, NO index 1.
    winner = None
    if len(payouts) >= 2:
        if payouts[0] == 1 and payouts[1] == 0:
            winner = "YES"
        elif payouts[0] == 0 and payouts[1] == 1:
            winner = "NO"

    condition_id = event.get("condition_id")
    if not condition_id:
        return

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Market)
            .where(Market.condition_id == condition_id.lower())
            .values(resolved=True, closed=True, winner=winner)
        )
        await session.commit()

    logger.info("Market {} resolved → winner={}", condition_id, winner)


async def _run_fastapi() -> None:
    s = get_settings()
    config = uvicorn.Config(
        fastapi_app, host=s.api_host, port=s.api_port, log_level=s.log_level.lower(), loop="asyncio"
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    settings = get_settings()
    logger.info("Polymarket bot main starting (mode={})", "PAPER" if settings.simulation_mode else "LIVE")

    ingestion = IngestionService()
    polygon = PolygonListener(on_resolution=_on_resolution)

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        stop_event.set()
        ingestion.stop()
        polygon.stop()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    tasks: list[asyncio.Task] = [  # type: ignore[type-arg]
        asyncio.create_task(ingestion.run(), name="ingestion"),
        asyncio.create_task(polygon.run(), name="polygon"),
        asyncio.create_task(_run_fastapi(), name="fastapi"),
    ]
    # Telegram is optional; if token is missing, do not add a short-lived task
    # that would terminate the whole process under FIRST_EXCEPTION policy.
    if settings.telegram_bot_token:
        tasks.append(asyncio.create_task(run_bot(), name="telegram"))
    else:
        logger.info("Telegram task disabled: TELEGRAM_BOT_TOKEN is not set")

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for t in pending:
        t.cancel()
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception) as e:  # noqa: BLE001
            logger.info("Task {} finished: {}", t.get_name(), e)


if __name__ == "__main__":
    asyncio.run(main())
