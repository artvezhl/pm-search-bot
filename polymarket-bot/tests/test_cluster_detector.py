"""Unit tests for cluster_detector.

We synthesise trade sequences and verify DBSCAN groups them the way we expect:
    * trades on the same market/outcome/price/time form a cluster
    * trades on different markets never merge
    * YES and NO outcomes never merge
    * solo traders do not produce clusters when min_samples=3
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.cluster_detector import detect_clusters


def _trade(
    addr: str,
    market: str,
    outcome: str,
    price: float,
    size: float,
    ts: datetime,
) -> dict:
    return {
        "maker_address": addr,
        "market_id": market,
        "outcome": outcome,
        "price": price,
        "size": size,
        "timestamp": ts,
    }


def test_three_players_same_market_form_cluster() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    trades = [
        _trade("0xA", "mkt1", "YES", 0.42, 1000, t0),
        _trade("0xB", "mkt1", "YES", 0.42, 1200, t0 + timedelta(minutes=2)),
        _trade("0xC", "mkt1", "YES", 0.43, 900, t0 + timedelta(minutes=4)),
    ]
    clusters = detect_clusters(trades, window_minutes=60, eps=0.5, min_samples=3)
    assert len(clusters) == 1
    c = clusters[0]
    assert c.market_id == "mkt1"
    assert c.outcome == "YES"
    assert set(c.leader_addrs) == {"0xa", "0xb", "0xc"} or set(c.leader_addrs) == {"0xA", "0xB", "0xC"}
    assert c.size == 3


def test_different_markets_never_merge() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    trades = [
        _trade("0xA", "mkt1", "YES", 0.42, 1000, t0),
        _trade("0xB", "mkt2", "YES", 0.42, 1200, t0),
        _trade("0xC", "mkt3", "YES", 0.42, 900, t0),
    ]
    clusters = detect_clusters(trades, window_minutes=60, eps=0.5, min_samples=3)
    assert clusters == []


def test_yes_and_no_never_merge() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    trades = [
        _trade("0xA", "mkt1", "YES", 0.42, 1000, t0),
        _trade("0xB", "mkt1", "YES", 0.42, 1200, t0),
        _trade("0xC", "mkt1", "NO", 0.42, 900, t0),
    ]
    clusters = detect_clusters(trades, window_minutes=60, eps=0.5, min_samples=3)
    assert clusters == []  # only 2 YES, below min_samples=3


def test_solo_trader_not_cluster() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    trades = [
        _trade("0xA", "mkt1", "YES", 0.42, 1000, t0),
        _trade("0xA", "mkt1", "YES", 0.42, 1100, t0 + timedelta(minutes=1)),
        _trade("0xA", "mkt1", "YES", 0.42, 900, t0 + timedelta(minutes=3)),
    ]
    clusters = detect_clusters(trades, window_minutes=60, eps=0.5, min_samples=3)
    # Same trader 3 times — unique players < min_samples.
    assert clusters == []
