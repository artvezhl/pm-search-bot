"""Exit Watcher — checks every open position against exit rules.

For each open position we consider three independent exit triggers:

    1. *leader_exit* — any of the cluster leaders sold their position.
       We detect this by looking for SELL trades of that address on the
       same market_id/outcome in the `trades` table after `opened_at`.

    2. *timeout* — `opened_at + MAX_HOLD_DAYS < now`.

    3. *stop_loss* — current price moved against us by more than
       `POSITION_STOP_LOSS_PCT`. Price is resolved via the outcome's
       `token_id` and CLOB /price endpoint.

The watcher does not execute trades itself — it returns a list of
ExitIntent which the trade executor acts upon.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.db.models import BotPosition, Market, Trade
from app.ingestion.clob_client import CLOBClient
from app.logger import logger


@dataclass(slots=True)
class ExitIntent:
    position: BotPosition
    reason: str  # "leader_exit" | "timeout" | "stop_loss"
    current_price: float | None


async def _fetch_open_positions(session: AsyncSession) -> list[BotPosition]:
    rows = await session.execute(
        select(BotPosition).where(BotPosition.status == "open")
    )
    return list(rows.scalars().all())


async def _market_for(session: AsyncSession, market_id: str) -> Market | None:
    r = await session.execute(select(Market).where(Market.condition_id == market_id))
    return r.scalars().first()


async def _has_leader_sold(
    session: AsyncSession, market_id: str, outcome: str, leaders: list[str], since: datetime
) -> bool:
    if not leaders:
        return False
    q = (
        select(Trade.trade_id)
        .where(Trade.market_id == market_id)
        .where(Trade.outcome == outcome)
        .where(Trade.side == "SELL")
        .where(Trade.maker_address.in_(leaders))
        .where(Trade.timestamp >= since)
        .limit(1)
    )
    r = await session.execute(q)
    return r.first() is not None


class ExitWatcher:
    def __init__(self) -> None:
        self._settings = get_settings()

    async def _current_price(self, position: BotPosition) -> float | None:
        if not position.token_id:
            return None
        try:
            async with CLOBClient() as clob:
                price = await clob.get_price(position.token_id, side="sell")
            return price
        except Exception as e:  # noqa: BLE001
            logger.debug("ExitWatcher: price fetch failed for token {}: {}", position.token_id, e)
            return None

    async def scan(self) -> list[ExitIntent]:
        s = self._settings
        now = datetime.now(tz=timezone.utc)
        intents: list[ExitIntent] = []

        async with AsyncSessionLocal() as session:
            positions = await _fetch_open_positions(session)

            for pos in positions:
                # 1) timeout
                if pos.opened_at and (now - pos.opened_at).days >= s.max_hold_days:
                    intents.append(ExitIntent(position=pos, reason="timeout", current_price=None))
                    continue

                # 2) leader_exit
                if s.exit_on_first_leader and pos.leader_addrs:
                    left = await _has_leader_sold(
                        session, pos.market_id, pos.outcome, pos.leader_addrs, pos.opened_at
                    )
                    if left:
                        intents.append(
                            ExitIntent(position=pos, reason="leader_exit", current_price=None)
                        )
                        continue

                # 3) stop_loss — fetched outside the DB session
                price = await self._current_price(pos)
                if price is None:
                    continue
                entry = float(pos.entry_price)
                if entry <= 0:
                    continue
                drop = (entry - price) / entry
                if drop >= s.position_stop_loss_pct:
                    intents.append(
                        ExitIntent(position=pos, reason="stop_loss", current_price=price)
                    )

        if intents:
            logger.info(
                "ExitWatcher: {} exit intents ({})",
                len(intents),
                ", ".join(i.reason for i in intents),
            )
        return intents
