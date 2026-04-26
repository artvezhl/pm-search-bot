"""Risk Manager — translates a signal into a position size subject to limits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.db.models import BotPosition
from app.logger import logger


@dataclass(slots=True)
class SizedOrder:
    size_usd: float
    accepted: bool
    reason: str


async def _current_balance(starting_balance: float) -> float:
    async with AsyncSessionLocal() as session:
        pnl_row = await session.execute(
            select(func.coalesce(func.sum(BotPosition.pnl_usd), 0)).where(
                BotPosition.status == "closed"
            )
        )
        realized_pnl = float(pnl_row.scalar() or 0)
        open_exposure_row = await session.execute(
            select(func.coalesce(func.sum(BotPosition.size_usd), 0)).where(
                BotPosition.status == "open"
            )
        )
        open_exposure = float(open_exposure_row.scalar() or 0)
        # Balance = starting + realized, available = balance - open exposure.
        return max(0.0, starting_balance + realized_pnl - open_exposure)


async def _open_positions_count() -> int:
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(func.count(BotPosition.id)).where(BotPosition.status == "open")
        )
        return int(r.scalar() or 0)


async def _today_realised_pnl_pct(starting_balance: float) -> float:
    since = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(func.coalesce(func.sum(BotPosition.pnl_usd), 0)).where(
                BotPosition.status == "closed", BotPosition.closed_at >= since
            )
        )
        pnl = float(r.scalar() or 0)
    return pnl / starting_balance if starting_balance else 0.0


async def _exposure_on_market(market_id: str) -> float:
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(func.coalesce(func.sum(BotPosition.size_usd), 0)).where(
                BotPosition.market_id == market_id, BotPosition.status == "open"
            )
        )
        return float(r.scalar() or 0)


class RiskManager:
    def __init__(self) -> None:
        self._settings = get_settings()

    async def size_for(self, market_id: str) -> SizedOrder:
        s = self._settings
        balance = await _current_balance(s.starting_balance)
        if balance <= 0:
            return SizedOrder(0.0, False, "no available balance")

        open_count = await _open_positions_count()
        if open_count >= s.max_open_positions:
            return SizedOrder(
                0.0, False, f"max_open_positions reached ({open_count})"
            )

        daily_pnl_pct = await _today_realised_pnl_pct(s.starting_balance)
        if daily_pnl_pct <= -s.max_daily_loss_pct:
            return SizedOrder(
                0.0, False, f"daily loss {daily_pnl_pct:.2%} <= -{s.max_daily_loss_pct:.0%}"
            )

        desired = balance * s.max_position_pct
        if desired < 1:
            return SizedOrder(0.0, False, f"size too small (${desired:.2f})")

        exposure = await _exposure_on_market(market_id)
        max_market_exposure = s.starting_balance * s.max_same_market_exposure
        remaining_market = max(0.0, max_market_exposure - exposure)
        if remaining_market < 1:
            return SizedOrder(
                0.0,
                False,
                f"market exposure cap reached (${exposure:.0f} >= ${max_market_exposure:.0f})",
            )

        size_usd = min(desired, remaining_market)
        logger.info(
            "Risk: market={} size=${:.2f} balance=${:.2f} open={} daily_pnl={:.2%}",
            market_id,
            size_usd,
            balance,
            open_count,
            daily_pnl_pct,
        )
        return SizedOrder(size_usd, True, "ok")
