"""Tests for PlayerTracker aggregation math (pure function, no DB)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.analysis.player_tracker import _compute_activity_score, aggregate_player


def _trade(market: str, outcome: str, price: float, size: float, side: str, ts: datetime) -> dict:
    return {
        "market_id": market,
        "outcome": outcome,
        "price": price,
        "size": size,
        "side": side,
        "timestamp": ts,
    }


def test_activity_score_spread() -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    days = [base + timedelta(days=i) for i in range(10)]
    score = _compute_activity_score(days)
    assert 0.9 <= score <= 1.0


def test_activity_score_single_day() -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    score = _compute_activity_score([base, base + timedelta(hours=2)])
    # single day => ratio = 1 / 1 = 1
    assert score == 1.0


def test_aggregate_winrate_and_pnl() -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trades = [
        _trade("mkt-a", "YES", 0.40, 1000, "BUY", base),
        _trade("mkt-b", "YES", 0.60, 500, "BUY", base + timedelta(hours=1)),
        _trade("mkt-c", "NO", 0.30, 2000, "BUY", base + timedelta(hours=2)),
    ]
    winners = {"mkt-a": "YES", "mkt-b": "NO", "mkt-c": "NO"}
    agg = aggregate_player(trades, winners)

    assert agg.trades_count == 3
    assert agg.markets_count == 3
    assert agg.total_volume_usd == 3500.0

    # Resolved trades: mkt-a won (YES), mkt-b lost (bought YES, winner NO), mkt-c won (NO).
    # winrate = 2/3
    assert abs(agg.winrate - 2 / 3) < 1e-6

    # PnL:
    # mkt-a: (1 - 0.4) * 1000  = +600
    # mkt-b: -0.6 * 500        = -300
    # mkt-c: (1 - 0.3) * 2000  = +1400
    # avg_profit = (600 - 300 + 1400) / 3 ≈ 566.666...
    assert abs(agg.avg_profit_usd - (1700 / 3)) < 1e-6


def test_aggregate_ignores_sell_side_for_winrate() -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trades = [
        _trade("mkt-a", "YES", 0.40, 1000, "SELL", base),
        _trade("mkt-a", "YES", 0.40, 1000, "BUY", base + timedelta(hours=1)),
    ]
    winners = {"mkt-a": "YES"}
    agg = aggregate_player(trades, winners)

    # Only BUY counts for winrate calc.
    assert agg.winrate == 1.0
