"""Cluster Detector — DBSCAN over recent trades from candidate players.

For every trade inside the lookback window we build a feature vector:
    [normalised_time, market_id_hash, outcome_flag, price, log(size)]

DBSCAN groups trades that are close in this space, i.e. same market, same
outcome, similar price, similar size, close in time. We require at least
`min_samples` **distinct players** to confirm a cluster (raw `min_samples`
is on trades; players-uniqueness is enforced after DBSCAN).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
from sklearn.cluster import DBSCAN
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.db.models import PlayerStats, Trade
from app.logger import logger


@dataclass(slots=True)
class ClusterCandidate:
    market_id: str
    outcome: str
    size: int
    leader_addrs: list[str]
    first_entry_ts: datetime
    avg_price: float
    total_volume_usd: float
    trades: list[dict] = field(default_factory=list)


def _hash_market(market_id: str) -> float:
    """Stable map market_id -> [0,1) so that trades on different markets
    land far apart in the feature space and DBSCAN can't merge them."""
    import hashlib

    h = hashlib.md5(market_id.encode("utf-8")).hexdigest()[:8]
    return int(h, 16) / 0xFFFFFFFF


def _normalise_time(ts: datetime, window_start: datetime, window_minutes: int) -> float:
    elapsed = (ts - window_start).total_seconds() / 60.0
    return max(0.0, min(1.0, elapsed / max(window_minutes, 1)))


def build_features(trades: list[dict], window_start: datetime, window_minutes: int) -> np.ndarray:
    """Return N x 5 feature matrix. Market is weighted so that trades on
    distinct markets end up >> `eps` apart regardless of other features."""
    rows: list[list[float]] = []
    for t in trades:
        ts = t["timestamp"]
        outcome_flag = 1.0 if t["outcome"].upper() == "YES" else 0.0
        size = max(float(t["size"]), 1e-6)
        rows.append(
            [
                _normalise_time(ts, window_start, window_minutes),
                _hash_market(t["market_id"]) * 1000.0,  # strong separator
                outcome_flag * 10.0,  # YES/NO never mix
                float(t["price"]),
                math.log(size),
            ]
        )
    return np.array(rows, dtype=float) if rows else np.zeros((0, 5))


def detect_clusters(
    trades: list[dict],
    window_minutes: int,
    eps: float,
    min_samples: int,
) -> list[ClusterCandidate]:
    if len(trades) < min_samples:
        return []

    trades_sorted = sorted(trades, key=lambda x: x["timestamp"])
    window_start = trades_sorted[0]["timestamp"]
    features = build_features(trades_sorted, window_start, window_minutes)

    model = DBSCAN(eps=eps, min_samples=min_samples, metric="euclidean")
    labels = model.fit_predict(features)

    by_label: dict[int, list[int]] = {}
    for idx, label in enumerate(labels):
        if label == -1:
            continue
        by_label.setdefault(int(label), []).append(idx)

    clusters: list[ClusterCandidate] = []
    for _, idxs in by_label.items():
        trade_group = [trades_sorted[i] for i in idxs]
        makers = {t["maker_address"] for t in trade_group}
        if len(makers) < min_samples:
            continue
        first_trade = trade_group[0]
        clusters.append(
            ClusterCandidate(
                market_id=first_trade["market_id"],
                outcome=first_trade["outcome"].upper(),
                size=len(makers),
                leader_addrs=sorted(makers),
                first_entry_ts=first_trade["timestamp"],
                avg_price=float(np.mean([t["price"] for t in trade_group])),
                total_volume_usd=float(sum(t["size"] for t in trade_group)),
                trades=trade_group,
            )
        )
    return clusters


async def _fetch_candidate_trades(
    session: AsyncSession, window_minutes: int
) -> list[dict]:
    since = datetime.now(tz=timezone.utc) - timedelta(minutes=window_minutes)
    candidates_subq = select(PlayerStats.address).where(PlayerStats.is_candidate.is_(True))
    q = (
        select(
            Trade.maker_address,
            Trade.market_id,
            Trade.outcome,
            Trade.price,
            Trade.size,
            Trade.timestamp,
        )
        .where(Trade.timestamp >= since)
        .where(Trade.side == "BUY")
        .where(Trade.maker_address.in_(candidates_subq))
    )
    rows = (await session.execute(q)).all()
    return [
        {
            "maker_address": r.maker_address,
            "market_id": r.market_id,
            "outcome": r.outcome,
            "price": float(r.price),
            "size": float(r.size),
            "timestamp": r.timestamp,
        }
        for r in rows
    ]


class ClusterDetector:
    def __init__(self) -> None:
        self._settings = get_settings()

    async def detect(self) -> list[ClusterCandidate]:
        s = self._settings
        async with AsyncSessionLocal() as session:
            trades = await _fetch_candidate_trades(session, s.time_window_minutes)

        clusters = detect_clusters(
            trades=trades,
            window_minutes=s.time_window_minutes,
            eps=s.dbscan_eps,
            min_samples=s.dbscan_min_samples,
        )

        if clusters:
            logger.info(
                "ClusterDetector: {} clusters on {} candidate trades",
                len(clusters),
                len(trades),
            )
        else:
            logger.debug(
                "ClusterDetector: no clusters on {} candidate trades", len(trades)
            )
        return clusters
