"""Tests for ingestion trade normalisation (pure function)."""

from __future__ import annotations

from app.ingestion.ingestion_service import normalize_trade


def test_normalize_full_payload() -> None:
    raw = {
        "id": "abc123",
        "market": "0xMARKET",
        "maker_address": "0xUSER",
        "outcome": "Yes",
        "price": "0.45",
        "size": "250.5",
        "side": "buy",
        "timestamp": 1700000000,
        "asset_id": "token-yes",
        "tx_hash": "0xTXN",
    }
    nt = normalize_trade(raw)
    assert nt is not None
    assert nt["trade_id"] == "abc123"
    assert nt["market_id"] == "0xmarket"
    assert nt["maker_address"] == "0xuser"
    assert nt["outcome"] == "YES"
    assert nt["side"] == "BUY"
    assert float(nt["price"]) == 0.45
    assert float(nt["size"]) == 250.5


def test_normalize_missing_required_returns_none() -> None:
    assert normalize_trade({"id": "x", "market": "m"}) is None
    assert normalize_trade({"id": "x", "maker_address": "u"}) is None
    assert normalize_trade({"market": "m", "maker_address": "u"}) is None


def test_normalize_alternative_field_names() -> None:
    raw = {
        "trade_id": "hash1",
        "condition_id": "0xABC",
        "maker": "0xX",
        "outcome": "NO",
        "price": 0.33,
        "shares": 10,
        "side": "SELL",
        "match_time": "2026-01-01T00:00:00Z",
    }
    nt = normalize_trade(raw)
    assert nt is not None
    assert nt["trade_id"] == "hash1"
    assert nt["market_id"] == "0xabc"
    assert nt["outcome"] == "NO"
    assert nt["side"] == "SELL"
    assert float(nt["size"]) == 10.0
