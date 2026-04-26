from __future__ import annotations

from app.analysis.network_detector import correlation_from_count


def test_correlation_from_count_bounds() -> None:
    assert correlation_from_count(-5) == 0.0
    assert correlation_from_count(5) == 0.5
    assert correlation_from_count(50) == 1.0
