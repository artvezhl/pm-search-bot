"""Paper executor — simulated fills stored in Postgres.

No real orders are sent. We record the entry/exit exactly as if they were
filled at the best ask/bid. PnL is computed on close.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db import AsyncSessionLocal
from app.db.models import BotPosition
from app.logger import logger
from app.signal.portfolio_state import close_position, open_position


@dataclass(slots=True)
class PaperFill:
    success: bool
    entry_price: float
    size_usd: float
    token_amount: float
    reason: str = "ok"


class PaperExecutor:
    async def open(
        self,
        *,
        market_id: str,
        outcome: str,
        token_id: str | None,
        price: float,
        size_usd: float,
        cluster_id: int | None,
        leader_addrs: list[str],
    ) -> tuple[PaperFill, BotPosition | None]:
        if price <= 0 or price >= 1:
            return PaperFill(False, price, size_usd, 0.0, "invalid price"), None

        token_amount = size_usd / price
        async with AsyncSessionLocal() as session:
            pos = await open_position(
                session=session,
                market_id=market_id,
                outcome=outcome,
                token_id=token_id,
                entry_price=price,
                size_usd=size_usd,
                token_amount=token_amount,
                cluster_id=cluster_id,
                leader_addrs=leader_addrs,
                simulation=True,
            )
            await session.commit()

        logger.info(
            "PAPER OPEN market={} outcome={} @ {:.4f} size=${:.2f} tokens={:.2f}",
            market_id,
            outcome,
            price,
            size_usd,
            token_amount,
        )
        return PaperFill(True, price, size_usd, token_amount), pos

    async def close(
        self, *, position: BotPosition, price: float, reason: str
    ) -> BotPosition:
        async with AsyncSessionLocal() as session:
            merged = await session.merge(position)
            updated = await close_position(
                session=session, position=merged, exit_price=price, exit_reason=reason
            )
            await session.commit()
            await session.refresh(updated)

        logger.info(
            "PAPER CLOSE market={} @ {:.4f} reason={} pnl=${}",
            position.market_id,
            price,
            reason,
            updated.pnl_usd,
        )
        return updated
