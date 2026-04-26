"""Signal Engine — decides whether a cluster should produce an ENTER signal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.behavior_scorer import BehaviorScorer
from app.analysis.cluster_detector import ClusterCandidate
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.db.models import Cluster, Market, Signal
from app.logger import logger
from app.signal.portfolio_state import get_open_position_for_market


@dataclass(slots=True)
class SignalDecision:
    decision: str  # "ENTER" | "SKIP_*"
    reason: str
    cluster_id: int | None
    score: float
    breakdown: dict[str, float]
    market: Market | None
    cluster: ClusterCandidate


async def _fetch_market(session: AsyncSession, market_id: str) -> Market | None:
    r = await session.execute(select(Market).where(Market.condition_id == market_id))
    return r.scalars().first()


async def persist_cluster(
    session: AsyncSession, cluster: ClusterCandidate, score: float
) -> int:
    row = Cluster(
        market_id=cluster.market_id,
        outcome=cluster.outcome,
        score=score,
        leader_addrs=cluster.leader_addrs,
        size=cluster.size,
        first_entry_ts=cluster.first_entry_ts,
        avg_price=cluster.avg_price,
        total_volume_usd=cluster.total_volume_usd,
    )
    session.add(row)
    await session.flush()
    return int(row.id)


async def persist_signal(
    session: AsyncSession,
    cluster_id: int,
    market_id: str,
    decision: str,
    reason: str,
    breakdown: dict[str, Any],
) -> None:
    session.add(
        Signal(
            cluster_id=cluster_id,
            market_id=market_id,
            decision=decision,
            reason=reason,
            metadata_json=breakdown,
        )
    )
    await session.flush()


class SignalEngine:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._scorer = BehaviorScorer()

    async def evaluate(self, cluster: ClusterCandidate) -> SignalDecision:
        s = self._settings
        score, breakdown = await self._scorer.score(cluster)

        async with AsyncSessionLocal() as session:
            cluster_id = await persist_cluster(session, cluster, score)
            market = await _fetch_market(session, cluster.market_id)
            await session.commit()

        # --- filters ---
        async def _finalize(decision: str, reason: str) -> SignalDecision:
            async with AsyncSessionLocal() as session:
                await persist_signal(
                    session, cluster_id, cluster.market_id, decision, reason, breakdown
                )
                await session.commit()
            logger.info(
                "Signal: {} cluster_id={} market={} reason={} score={:.3f}",
                decision,
                cluster_id,
                cluster.market_id,
                reason,
                score,
            )
            return SignalDecision(
                decision=decision,
                reason=reason,
                cluster_id=cluster_id,
                score=score,
                breakdown=breakdown,
                market=market,
                cluster=cluster,
            )

        if cluster.size < s.min_cluster_size:
            return await _finalize("SKIP_CLUSTER_SIZE", f"size={cluster.size} < {s.min_cluster_size}")

        if score < s.min_cluster_score:
            return await _finalize("SKIP_SCORE", f"score={score:.3f} < {s.min_cluster_score}")

        avg_pos = cluster.total_volume_usd / max(cluster.size, 1)
        if avg_pos < s.min_position_usd:
            return await _finalize(
                "SKIP_POSITION", f"avg_pos=${avg_pos:.0f} < ${s.min_position_usd}"
            )

        lag_min = (datetime.now(tz=timezone.utc) - cluster.first_entry_ts).total_seconds() / 60.0
        if lag_min > s.max_entry_lag_min:
            return await _finalize(
                "SKIP_LAG", f"lag={lag_min:.1f}min > {s.max_entry_lag_min}"
            )

        if market is None:
            return await _finalize("SKIP_UNKNOWN_MARKET", "market row missing")

        if market.liquidity_usd is not None and float(market.liquidity_usd) < s.min_market_liquidity:
            return await _finalize(
                "SKIP_LIQUIDITY", f"liquidity=${float(market.liquidity_usd):.0f} < ${s.min_market_liquidity}"
            )

        existing = await get_open_position_for_market(cluster.market_id)
        if existing is not None:
            return await _finalize("SKIP_DUPLICATE", "already have open position")

        return await _finalize("ENTER", "all filters passed")
