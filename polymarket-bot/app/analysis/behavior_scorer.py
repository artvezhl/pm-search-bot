"""Behaviour Scorer — assign a confidence score to a cluster.

Per ТЗ:
    score = 0.4 * elite_ratio + 0.4 * sync_exit_score + 0.2 * size_score

    elite_ratio     — share of cluster players with winrate >= MIN_WINRATE
    sync_exit_score — proxy: stdev of historical exit lags, normalised
    size_score      — normalised mean position size of the cluster (log-scale)

For MVP the sync_exit_score is a simplified proxy: we compute winrate
correlation among the leaders as a heuristic (same-winrate leaders tend
to behave similarly). A precise exit-synchrony metric requires storing
per-position exit timestamps, which we introduce in v2.
"""

from __future__ import annotations

import math

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.cluster_detector import ClusterCandidate
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.db.models import PlayerStats


async def _fetch_player_stats(
    session: AsyncSession, addresses: list[str]
) -> dict[str, PlayerStats]:
    if not addresses:
        return {}
    rows = (
        await session.execute(
            select(PlayerStats).where(PlayerStats.address.in_(addresses))
        )
    ).scalars().all()
    return {r.address: r for r in rows}


def _elite_ratio(stats: list[PlayerStats], min_winrate: float) -> float:
    if not stats:
        return 0.0
    qualified = sum(1 for s in stats if float(s.winrate) >= min_winrate)
    return qualified / len(stats)


def _sync_exit_score(stats: list[PlayerStats]) -> float:
    """Proxy: low variance of winrates => more synchronised behaviour."""
    if len(stats) < 2:
        return 0.5
    wrs = [float(s.winrate) for s in stats]
    mean = sum(wrs) / len(wrs)
    var = sum((w - mean) ** 2 for w in wrs) / len(wrs)
    std = math.sqrt(var)
    # std in [0, ~0.5] => map to [1, 0] roughly
    return max(0.0, min(1.0, 1.0 - (std / 0.5)))


def _size_score(cluster_avg_size_usd: float) -> float:
    """Log-normalise mean position size to [0, 1]. $100 -> 0.0, $10k -> ~1.0."""
    if cluster_avg_size_usd <= 100:
        return 0.0
    return max(0.0, min(1.0, (math.log10(cluster_avg_size_usd) - 2.0) / 2.0))


class BehaviorScorer:
    def __init__(self) -> None:
        self._settings = get_settings()

    async def score(self, cluster: ClusterCandidate) -> tuple[float, dict[str, float]]:
        async with AsyncSessionLocal() as session:
            stats_by_addr = await _fetch_player_stats(session, cluster.leader_addrs)

        leader_stats = [stats_by_addr[a] for a in cluster.leader_addrs if a in stats_by_addr]

        elite_ratio = _elite_ratio(leader_stats, self._settings.min_winrate)
        sync_exit = _sync_exit_score(leader_stats)
        avg_size = cluster.total_volume_usd / max(cluster.size, 1)
        size_score = _size_score(avg_size)

        final = 0.4 * elite_ratio + 0.4 * sync_exit + 0.2 * size_score
        breakdown = {
            "elite_ratio": round(elite_ratio, 4),
            "sync_exit_score": round(sync_exit, 4),
            "size_score": round(size_score, 4),
            "score": round(final, 4),
        }
        return final, breakdown
