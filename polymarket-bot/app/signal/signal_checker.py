from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import select

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.db.models import PMCopySignal, PMMarket, PMTrade, PMWallet


def calculate_copy_amount(smart_score: float, trigger_amount: float) -> float:
    s = get_settings()
    base_allocation = 0.05
    scale = 0.5 + smart_score
    return min(s.bot_capital_usdc * base_allocation * scale, s.max_single_position_usdc, max(trigger_amount * 0.2, 1.0))


class SignalChecker:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._redis = Redis.from_url(self._settings.redis_url, decode_responses=True)

    async def check_and_emit_signal(self, trade: PMTrade) -> PMCopySignal | None:
        s = self._settings
        async with AsyncSessionLocal() as session:
            wallet = (
                await session.execute(
                    select(PMWallet).where(PMWallet.address == trade.maker_address, PMWallet.is_smart_wallet.is_(True))
                )
            ).scalars().first()
            if not wallet:
                return None

            market = (
                await session.execute(select(PMMarket).where(PMMarket.condition_id == trade.condition_id))
            ).scalars().first()
            if not market or market.resolved:
                return None
            if float(trade.price) > s.pm_copy_max_price:
                return None
            if market.close_time:
                hours_to_close = (market.close_time - datetime.now(tz=timezone.utc)).total_seconds() / 3600
                if hours_to_close < s.pm_copy_min_hours_to_close:
                    return None

            suggested_amount = calculate_copy_amount(float(wallet.smart_score or 0), float(trade.amount_usdc))
            signal = PMCopySignal(
                trigger_wallet=trade.maker_address,
                trigger_tx_hash=trade.tx_hash,
                trigger_trade_id=trade.id,
                condition_id=trade.condition_id,
                token_outcome=trade.token_outcome,
                action="BUY",
                suggested_price=Decimal(str(round(float(trade.price) + 0.01, 6))),
                suggested_amount=Decimal(str(round(suggested_amount, 2))),
                status="PENDING",
            )
            session.add(signal)
            await session.flush()
            await session.commit()
            await self._redis.lpush(
                "polymarket:signals",
                json.dumps(
                    {
                        "signal_id": signal.id,
                        "condition_id": signal.condition_id,
                        "token_outcome": signal.token_outcome,
                        "suggested_price": float(signal.suggested_price or 0),
                        "suggested_amount": float(signal.suggested_amount or 0),
                        "trigger_wallet": signal.trigger_wallet,
                    }
                ),
            )
            return signal
