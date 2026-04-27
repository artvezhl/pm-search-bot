from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import AsyncSessionLocal
from app.db.models import PMMarket, PMTrade
from app.ingestion.dune_client import DuneClient
from app.logger import logger


class DuneHistoryLoader:
    """Load PM trades from Dune query results into pm_trades."""

    @staticmethod
    def _pick(row: dict[str, Any], *keys: str) -> Any:
        for k in keys:
            if k in row and row[k] is not None:
                return row[k]
        return None

    @staticmethod
    def _to_dt(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value
        s = str(value).strip()
        if not s:
            return None
        if s.endswith(" UTC"):
            s = s[:-4] + "+00:00"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def _to_decimal(value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    def _to_row(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        tx_hash = self._pick(raw, "tx_hash", "transaction_hash")
        maker = self._pick(raw, "maker_address", "maker")
        block_time = self._to_dt(self._pick(raw, "block_time", "timestamp", "time"))
        condition_id = self._pick(raw, "condition_id", "market_id")
        token_outcome = self._pick(raw, "token_outcome", "outcome")
        taker = self._pick(raw, "taker_address", "taker")
        action = self._pick(raw, "action", "side")

        price = self._to_decimal(self._pick(raw, "price"))
        amount_usdc = self._to_decimal(self._pick(raw, "amount_usdc", "size", "notional_usdc"))
        shares = self._to_decimal(self._pick(raw, "shares", "amount", "size_shares"))

        if not tx_hash or not maker or block_time is None or price is None or amount_usdc is None:
            return None

        cid = str(condition_id).lower() if condition_id else None
        outcome = str(token_outcome).upper() if token_outcome else None
        side = str(action).upper() if action else None

        if price <= 0 or amount_usdc <= 0:
            return None

        return {
            "tx_hash": str(tx_hash),
            "block_time": block_time,
            "condition_id": cid,
            "token_outcome": outcome,
            "maker_address": str(maker).lower(),
            "taker_address": str(taker).lower() if taker else None,
            "price": price,
            "amount_usdc": amount_usdc,
            "shares": shares,
            "action": side,
            "raw_data": raw,
        }

    async def _insert_rows(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0

        chunk_size = 2_000
        inserted = 0
        async with AsyncSessionLocal() as session:
            condition_ids = {r["condition_id"] for r in rows if r.get("condition_id")}
            if condition_ids:
                market_stmt = pg_insert(PMMarket).values(
                    [{"condition_id": condition_id} for condition_id in condition_ids]
                )
                await session.execute(
                    market_stmt.on_conflict_do_nothing(index_elements=["condition_id"])
                )
            for i in range(0, len(rows), chunk_size):
                chunk = rows[i : i + chunk_size]
                stmt = pg_insert(PMTrade).values(chunk)
                result = await session.execute(
                    stmt.on_conflict_do_nothing(
                        index_elements=["tx_hash", "condition_id", "maker_address"]
                    )
                )
                inserted += int(result.rowcount or 0)
            await session.commit()
        return inserted

    async def run(
        self,
        query_id: int,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        page_limit: int = 30_000,
    ) -> int:
        params: dict[str, Any] = {}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to

        total_inserted = 0
        total_seen = 0
        next_uri: str | None = None

        async with DuneClient() as dune:
            execution_id = await dune.execute_query(query_id=query_id, parameters=params or None)
            await dune.wait_until_completed(execution_id)
            page = await dune.get_results(execution_id=execution_id, limit=page_limit, offset=0)

            while True:
                result = page.get("result") or {}
                rows = result.get("rows") or []
                mapped_rows = [r for r in (self._to_row(row) for row in rows) if r]
                inserted = await self._insert_rows(mapped_rows)

                total_seen += len(rows)
                total_inserted += inserted
                logger.info(
                    "DuneHistoryLoader page processed: rows={} mapped={} inserted={} total_inserted={}",
                    len(rows),
                    len(mapped_rows),
                    inserted,
                    total_inserted,
                )

                next_uri = (
                    result.get("next_uri")
                    or result.get("nextUri")
                    or (page.get("next_uri") if isinstance(page, dict) else None)
                )
                if not next_uri:
                    break
                page = await dune.get_results_by_next_uri(str(next_uri))

        logger.info(
            "DuneHistoryLoader finished: query_id={} total_seen={} total_inserted={}",
            query_id,
            total_seen,
            total_inserted,
        )
        return total_inserted
