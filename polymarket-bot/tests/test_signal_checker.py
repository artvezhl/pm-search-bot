from __future__ import annotations

from app.signal.signal_checker import calculate_copy_amount


def test_calculate_copy_amount_positive_and_capped() -> None:
    amount = calculate_copy_amount(0.8, 120.0)
    assert amount > 0
    assert amount <= 50
