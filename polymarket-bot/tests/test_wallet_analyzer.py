from __future__ import annotations

from app.analysis.wallet_analyzer import WalletMetrics, calculate_smart_score


def test_calculate_smart_score_filters_out_low_resolved() -> None:
    m = WalletMetrics(
        win_rate=0.8,
        avg_entry_price=0.2,
        avg_roi=1.5,
        total_volume_usdc=10_000,
        resolved_markets=10,
        active_last_30d=True,
    )
    assert calculate_smart_score(m, min_resolved=15) == 0.0


def test_calculate_smart_score_high_quality_wallet() -> None:
    m = WalletMetrics(
        win_rate=0.78,
        avg_entry_price=0.18,
        avg_roi=1.2,
        total_volume_usdc=20_000,
        resolved_markets=40,
        active_last_30d=True,
    )
    score = calculate_smart_score(m, min_resolved=15)
    assert 0.55 <= score <= 1.0
