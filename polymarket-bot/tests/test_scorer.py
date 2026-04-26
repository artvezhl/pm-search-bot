"""Pure-function tests for BehaviorScorer helpers."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.analysis.behavior_scorer import _elite_ratio, _size_score, _sync_exit_score


def _ps(winrate: float) -> SimpleNamespace:
    return SimpleNamespace(winrate=Decimal(str(winrate)))


def test_elite_ratio_all_above() -> None:
    stats = [_ps(0.7), _ps(0.65), _ps(0.80)]
    assert _elite_ratio(stats, 0.6) == 1.0


def test_elite_ratio_partial() -> None:
    stats = [_ps(0.55), _ps(0.65), _ps(0.40), _ps(0.80)]
    assert abs(_elite_ratio(stats, 0.6) - 0.5) < 1e-6


def test_elite_ratio_empty() -> None:
    assert _elite_ratio([], 0.6) == 0.0


def test_sync_exit_perfect() -> None:
    stats = [_ps(0.7), _ps(0.7), _ps(0.7)]
    assert _sync_exit_score(stats) == 1.0


def test_sync_exit_diverse() -> None:
    stats = [_ps(0.2), _ps(0.9)]
    score = _sync_exit_score(stats)
    assert 0.0 <= score <= 0.5


def test_size_score_scaling() -> None:
    assert _size_score(50) == 0.0
    assert 0.45 <= _size_score(1_000) <= 0.55
    assert 0.95 <= _size_score(10_000) <= 1.0
