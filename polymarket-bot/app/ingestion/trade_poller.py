from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.db.models import PMMarket, PMTrade
from app.ingestion.clob_client import CLOBClient, parse_trade_timestamp
from app.signal.signal_checker import SignalChecker


class TradePoller:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._redis = Redis.from_url(self._settings.redis_url, decode_responses=True)
        self._signal_checker = SignalChecker()

    async def _get_cursor(self, condition_id: str) -> str:
        return await self._redis.get(f"pm:cursor:{condition_id}") or ""

    async def _set_cursor(self, condition_id: str, cursor: str) -> None:
        await self._redis.set(f"pm:cursor:{condition_id}", cursor)

    def _normalize(self, raw: dict[str, Any], condition_id: str) -> dict[str, Any] | None:
        tx_hash = raw.get("transaction_hash") or raw.get("tx_hash")
        maker = raw.get("maker_address") or raw.get("maker")
        if not tx_hash or not maker:
            return None
        outcome = str(raw.get("outcome") or "").upper()
        if outcome in {"UP", "TRUE", "1"}:
            outcome = "YES"
        elif outcome in {"DOWN", "FALSE", "0"}:
            outcome = "NO"
        return {
            "tx_hash": str(tx_hash),
            "block_time": parse_trade_timestamp(
                raw.get("timestamp") or raw.get("match_time") or raw.get("last_update")
            )
            or datetime.now(tz=timezone.utc),
            "condition_id": condition_id,
            "token_outcome": outcome,
            "maker_address": str(maker).lower(),
            "taker_address": str(raw.get("taker_address") or "").lower() or None,
            "price": Decimal(str(raw.get("price") or 0)),
            "amount_usdc": Decimal(str(raw.get("size") or 0)),
            "shares": Decimal(str(raw.get("size") or 0)),
            "action": "CLOB",
            "raw_data": raw,
        }

    async def poll_once(self) -> int:
        async with AsyncSessionLocal() as session:
            condition_ids = (
                await session.execute(
                    select(PMMarket.condition_id).where(PMMarket.resolved.is_(False))
                )
            ).scalars().all()
        sem = asyncio.Semaphore(self._settings.pm_max_parallel_market_polls)
        inserted = 0

        async def process_market(condition_id: str) -> int:
            async with sem:
                cursor = await self._get_cursor(condition_id)
                async with CLOBClient() as clob:
                    # `fetch_trades` prefers `/data/trades` and gracefully falls back.
                    trades = await clob.fetch_trades(market_id=condition_id, limit=500)
                next_cursor = cursor
                rows = [self._normalize(t, condition_id) for t in trades]
                rows = [r for r in rows if r]
                if not rows:
                    return 0
                async with AsyncSessionLocal() as inner:
                    stmt = pg_insert(PMTrade).values(rows)
                    await inner.execute(
                        stmt.on_conflict_do_nothing(index_elements=["tx_hash", "condition_id", "maker_address"])
                    )
                    await inner.commit()
                    stored = (
                        await inner.execute(
                            select(PMTrade).where(PMTrade.tx_hash.in_([r["tx_hash"] for r in rows]))
                        )
                    ).scalars().all()
                    for tr in stored:
                        await self._signal_checker.check_and_emit_signal(tr)
                await self._set_cursor(condition_id, str(next_cursor))
                return len(rows)

        for count in await asyncio.gather(*[process_market(cid) for cid in condition_ids[:200]], return_exceptions=False):
            inserted += int(count)
        if inserted == 0:
            inserted += await self._ingest_global_fallback(set(condition_ids))
        return inserted

    async def _ingest_global_fallback(self, known_markets: set[str]) -> int:
        """Fallback for environments where market-scoped endpoint returns empty lists."""
        async with CLOBClient() as clob:
            trades = await clob.fetch_trades(limit=500)
        rows = []
        new_markets = set()
        for t in trades:
            condition_id = str(t.get("market") or t.get("condition_id") or "").lower()
            if not condition_id:
                continue
            nt = self._normalize(t, condition_id)
            if nt:
                rows.append(nt)
                if condition_id not in known_markets:
                    new_markets.add(condition_id)
        if not rows:
            return 0
        async with AsyncSessionLocal() as session:
            if new_markets:
                market_rows = [{"condition_id": m, "question": "auto-seeded from global trades"} for m in sorted(new_markets)]
                m_stmt = pg_insert(PMMarket).values(market_rows).on_conflict_do_nothing(index_elements=["condition_id"])
                await session.execute(m_stmt)
            stmt = pg_insert(PMTrade).values(rows).on_conflict_do_nothing(
                index_elements=["tx_hash", "condition_id", "maker_address"]
            )
            result = await session.execute(stmt)
            await session.commit()
            stored = (
                await session.execute(
                    select(PMTrade).where(PMTrade.tx_hash.in_([r["tx_hash"] for r in rows]))
                )
            ).scalars().all()
            for tr in stored:
                await self._signal_checker.check_and_emit_signal(tr)
        return int(result.rowcount or 0)
