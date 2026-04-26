"""Player Tracker — per-address behavioural aggregates.

Input: raw trades from the `trades` hypertable (last N days).
Output: upserted rows in `player_stats` with booleans `is_candidate`.

Winrate approximation (MVP):
    For every maker we collect BUY trades and, for markets whose winner
    is already known (`markets.winner`), mark the trade as win if it
    bought the winning outcome. Incomplete positions (markets still open)
    are not counted. This is an approximation because position sizing /
    partial exits are ignored, but it's sufficient for filter candidacy.

For resolved markets we compute avg_profit_usd as:
    (1.0 - entry_price) * size    if bought winner
    (   - entry_price) * size    if bought loser
    where size is USDC notional and entry_price is the trade price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.db.models import Market, PlayerStats, Trade
from app.logger import logger


@dataclass(slots=True)
class PlayerAggregate:
    address: str
    trades_count: int
    markets_count: int
    total_volume_usd: float
    avg_position_usd: float
    winrate: float
    avg_profit_usd: float
    activity_score: float


def _compute_activity_score(trade_days: list[datetime]) -> float:
    """Higher when trades are spread uniformly over the lookback window.

    Returns a value in [0, 1]. We bucket trades by day and use
    (unique_days / total_days). Small number of days caps the score.
    """
    if not trade_days:
        return 0.0
    unique_days = {d.date() for d in trade_days}
    span_days = (max(trade_days).date() - min(trade_days).date()).days + 1
    if span_days <= 0:
        return 0.0
    ratio = len(unique_days) / span_days
    # clamp 0..1
    return float(min(max(ratio, 0.0), 1.0))


async def _fetch_player_trades(
    session: AsyncSession, since: datetime
) -> dict[str, list[dict]]:
    """Group trades by maker_address for the lookback window."""
    q = select(
        Trade.trade_id,
        Trade.market_id,
        Trade.maker_address,
        Trade.outcome,
        Trade.price,
        Trade.size,
        Trade.side,
        Trade.timestamp,
    ).where(Trade.timestamp >= since)

    rows = (await session.execute(q)).all()
    by_addr: dict[str, list[dict]] = {}
    for r in rows:
        by_addr.setdefault(r.maker_address, []).append(
            {
                "trade_id": r.trade_id,
                "market_id": r.market_id,
                "outcome": r.outcome,
                "price": float(r.price),
                "size": float(r.size),
                "side": r.side,
                "timestamp": r.timestamp,
            }
        )
    return by_addr


async def _fetch_market_winners(session: AsyncSession) -> dict[str, str]:
    rows = (
        await session.execute(
            select(Market.condition_id, Market.winner).where(Market.winner.isnot(None))
        )
    ).all()
    return {mid: winner for mid, winner in rows}


def aggregate_player(trades: list[dict], winners: dict[str, str]) -> PlayerAggregate:
    address = trades[0]["maker_address"] if trades and "maker_address" in trades[0] else ""
    if not address and trades:
        # aggregate_player is often called with rows that omit maker_address key
        address = trades[0].get("address", "")

    markets = {t["market_id"] for t in trades}
    volumes = [t["size"] for t in trades]
    total_volume = float(sum(volumes))
    avg_position = total_volume / len(trades) if trades else 0.0

    # Winrate + PnL proxy — only BUY trades on resolved markets.
    resolved_trades = [t for t in trades if t["side"] == "BUY" and t["market_id"] in winners]
    wins = 0
    pnl_sum = 0.0
    for t in resolved_trades:
        winner_outcome = winners[t["market_id"]]
        bought_winner = t["outcome"] == winner_outcome
        if bought_winner:
            wins += 1
            pnl_sum += (1.0 - t["price"]) * t["size"]
        else:
            pnl_sum -= t["price"] * t["size"]
    winrate = wins / len(resolved_trades) if resolved_trades else 0.0
    avg_profit = pnl_sum / len(resolved_trades) if resolved_trades else 0.0

    return PlayerAggregate(
        address=address,
        trades_count=len(trades),
        markets_count=len(markets),
        total_volume_usd=total_volume,
        avg_position_usd=avg_position,
        winrate=winrate,
        avg_profit_usd=avg_profit,
        activity_score=_compute_activity_score([t["timestamp"] for t in trades]),
    )


class PlayerTracker:
    def __init__(self) -> None:
        self._settings = get_settings()

    def is_candidate(self, agg: PlayerAggregate) -> bool:
        s = self._settings
        if agg.trades_count < s.min_trades_count:
            return False
        if agg.total_volume_usd < s.min_total_volume_usd:
            return False
        if agg.winrate < s.min_winrate:
            return False
        return True

    async def refresh(self) -> int:
        """Recompute `player_stats` for every active maker in the lookback.

        Returns number of rows upserted.
        """
        s = self._settings
        since = datetime.now(tz=timezone.utc) - timedelta(days=s.player_stats_lookback_days)

        async with AsyncSessionLocal() as session:
            by_addr = await _fetch_player_trades(session, since)
            winners = await _fetch_market_winners(session)

            if not by_addr:
                logger.info("PlayerTracker: no trades in window, skipping")
                return 0

            rows = []
            for addr, trades in by_addr.items():
                for t in trades:
                    t["maker_address"] = addr
                agg = aggregate_player(trades, winners)
                agg.address = addr
                rows.append(
                    {
                        "address": agg.address,
                        "winrate": Decimal(str(round(agg.winrate, 4))),
                        "avg_profit_usd": Decimal(str(round(agg.avg_profit_usd, 6))),
                        "total_volume_usd": Decimal(str(round(agg.total_volume_usd, 6))),
                        "avg_position_usd": Decimal(str(round(agg.avg_position_usd, 6))),
                        "activity_score": Decimal(str(round(agg.activity_score, 4))),
                        "trades_count": agg.trades_count,
                        "markets_count": agg.markets_count,
                        "is_candidate": self.is_candidate(agg),
                    }
                )

            if not rows:
                return 0

            stmt = pg_insert(PlayerStats).values(rows)
            update_cols = {
                c.name: getattr(stmt.excluded, c.name)
                for c in PlayerStats.__table__.columns
                if c.name not in {"address"}
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=["address"], set_=update_cols
            )
            await session.execute(stmt)
            await session.commit()

            candidates = sum(1 for r in rows if r["is_candidate"])
            logger.info(
                "PlayerTracker: updated {} players, {} candidates", len(rows), candidates
            )
            return len(rows)
