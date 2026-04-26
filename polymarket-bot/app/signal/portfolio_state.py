"""Portfolio state — persistence helpers around `bot_positions`."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.db.models import BotPosition


async def get_open_positions() -> list[BotPosition]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(BotPosition).where(BotPosition.status == "open")
            )
        ).scalars().all()
        return list(rows)


async def open_position(
    *,
    session: AsyncSession,
    market_id: str,
    outcome: str,
    token_id: str | None,
    entry_price: float,
    size_usd: float,
    token_amount: float,
    cluster_id: int | None,
    leader_addrs: list[str],
    simulation: bool,
) -> BotPosition:
    pos = BotPosition(
        market_id=market_id,
        outcome=outcome,
        token_id=token_id,
        entry_price=Decimal(str(entry_price)),
        size_usd=Decimal(str(size_usd)),
        token_amount=Decimal(str(token_amount)),
        cluster_id=cluster_id,
        leader_addrs=leader_addrs,
        status="open",
        simulation=simulation,
    )
    session.add(pos)
    await session.flush()
    return pos


async def close_position(
    *,
    session: AsyncSession,
    position: BotPosition,
    exit_price: float,
    exit_reason: str,
) -> BotPosition:
    position.status = "closed"
    position.exit_price = Decimal(str(exit_price))
    position.exit_reason = exit_reason
    position.closed_at = datetime.now(tz=timezone.utc)
    position.pnl_usd = Decimal(str((exit_price - float(position.entry_price)) * float(position.token_amount)))
    await session.flush()
    return position


async def total_exposure_on_market(market_id: str) -> float:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(func.coalesce(func.sum(BotPosition.size_usd), 0)).where(
                BotPosition.market_id == market_id, BotPosition.status == "open"
            )
        )
        return float(result.scalar() or 0)


async def count_open_positions() -> int:
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(func.count(BotPosition.id)).where(BotPosition.status == "open")
        )
        return int(r.scalar() or 0)


async def get_open_position_for_market(market_id: str) -> BotPosition | None:
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(BotPosition).where(
                BotPosition.market_id == market_id, BotPosition.status == "open"
            )
        )
        return r.scalars().first()
