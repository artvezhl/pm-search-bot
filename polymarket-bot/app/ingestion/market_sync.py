from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import AsyncSessionLocal
from app.db.models import PMMarket, PMTrade
from app.ingestion.clob_client import CLOBClient
from app.logger import logger


TARGET_KEYWORDS = (
    "champions league",
    "nba",
    "nfl",
    "premier league",
    "bitcoin",
    "ethereum",
    "btc",
    "eth",
)


def _to_decimal_bool_winner(value: object) -> Decimal | None:
    s = str(value or "").strip().upper()
    if s in {"YES", "TRUE", "1"}:
        return Decimal("1.0")
    if s in {"NO", "FALSE", "0"}:
        return Decimal("0.0")
    return None


def _extract_resolution_value(raw: dict) -> Decimal | None:
    # 1) Direct winner field on market payload.
    direct = _to_decimal_bool_winner(raw.get("winner"))
    if direct is not None:
        return direct

    # 2) Winner marker inside token entries.
    tokens = raw.get("tokens") or []
    for t in tokens:
        token_winner = t.get("winner")
        if token_winner is None:
            continue
        outcome = str(t.get("outcome") or "").upper()
        if str(token_winner).strip().lower() in {"true", "1", "yes"}:
            if outcome == "YES":
                return Decimal("1.0")
            if outcome == "NO":
                return Decimal("0.0")

    # 3) Derive from outcome prices when market is resolved (one side is ~1).
    outcome_prices = raw.get("outcome_prices") or raw.get("outcomePrices") or []
    if isinstance(outcome_prices, list) and len(outcome_prices) >= 2:
        try:
            yes_p = float(outcome_prices[0])
            no_p = float(outcome_prices[1])
            if yes_p >= 0.999 or no_p <= 0.001:
                return Decimal("1.0")
            if no_p >= 0.999 or yes_p <= 0.001:
                return Decimal("0.0")
        except Exception:
            pass
    return None


def _is_target_market(raw: dict) -> bool:
    text = f"{raw.get('question', '')} {raw.get('description', '')} {raw.get('event_market_name', '')} {raw.get('market_slug', '')}".lower()
    if not any(k in text for k in TARGET_KEYWORDS):
        return False
    volume = float(raw.get("volume") or raw.get("volumeNum") or 0)
    if volume and volume < 10_000:
        return False
    return True


def _map_market(raw: dict) -> dict:
    condition_id = str(raw.get("condition_id") or raw.get("conditionId") or "").lower()
    slug = raw.get("market_slug") or raw.get("slug") or ""
    token_id = None
    tokens = raw.get("tokens") or []
    if tokens:
        token_id = tokens[0].get("token_id") or tokens[0].get("tokenId") or tokens[0].get("asset_id")
    resolved = bool(
        raw.get("closed", False)
        or raw.get("resolved", False)
        or str(raw.get("market_status", "")).lower() in {"resolved", "finalized", "closed"}
    )
    resolution_value = _extract_resolution_value(raw)
    return {
        "condition_id": condition_id,
        "event_market_name": raw.get("event_market_name") or raw.get("description") or raw.get("question"),
        "question": raw.get("question") or raw.get("description"),
        "token_outcome": None,
        "asset_id": token_id,
        "neg_risk": bool(raw.get("neg_risk", False)),
        "polymarket_link": f"https://polymarket.com/event/{slug}" if slug else None,
        "resolved": resolved,
        "resolution_value": resolution_value,
        "close_time": (
            datetime.fromisoformat(str(raw.get("end_date_iso") or raw.get("endDate") or "").replace("Z", "+00:00"))
            if (raw.get("end_date_iso") or raw.get("endDate"))
            else None
        ),
    }


class MarketSync:
    async def _upsert_rows(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        chunk_size = 500
        total = 0
        async with AsyncSessionLocal() as session:
            for i in range(0, len(rows), chunk_size):
                chunk = rows[i : i + chunk_size]
                stmt = pg_insert(PMMarket).values(chunk)
                update_cols = {
                    c.name: getattr(stmt.excluded, c.name)
                    for c in PMMarket.__table__.columns
                    if c.name != "condition_id"
                }
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["condition_id"],
                        set_=update_cols,
                    )
                )
                total += len(chunk)
            await session.commit()
        return total

    async def refresh(self) -> int:
        async with CLOBClient() as clob:
            markets = await clob.fetch_active_markets(max_pages=20)
        rows = [
            _map_market(m)
            for m in markets
            if (m.get("conditionId") or m.get("condition_id")) and _is_target_market(m)
        ]
        # Fallback bootstrap: when topical filters yield too few markets, keep a
        # broad active slice so the poller can actually ingest trades.
        if len(rows) < 100:
            rows = [
                _map_market(m)
                for m in markets[:500]
                if (m.get("conditionId") or m.get("condition_id"))
            ]
        total = await self._upsert_rows(rows)
        logger.info("MarketSync: upserted {} markets", total)
        return total

    async def refresh_traded_metadata(self, max_pages: int = 200) -> int:
        async with AsyncSessionLocal() as session:
            traded_conditions = set(
                str(x).lower()
                for x in (
                    await session.execute(
                        select(PMTrade.condition_id).where(PMTrade.condition_id.is_not(None)).distinct()
                    )
                ).scalars()
            )
        if not traded_conditions:
            return 0

        found: dict[str, dict] = {}
        async with CLOBClient() as clob:
            cursor = ""
            for _ in range(max_pages):
                page = await clob.fetch_markets(cursor)
                if isinstance(page, list):
                    data = page
                    next_cursor = ""
                elif isinstance(page, dict):
                    data = page.get("data") or page.get("markets") or []
                    next_cursor = page.get("next_cursor") or page.get("nextCursor") or ""
                else:
                    data = []
                    next_cursor = ""

                for raw in data:
                    condition_id = str(raw.get("condition_id") or raw.get("conditionId") or "").lower()
                    if condition_id and condition_id in traded_conditions:
                        found[condition_id] = _map_market(raw)
                if not next_cursor or next_cursor == "LTE=":
                    break
                cursor = next_cursor

        total = await self._upsert_rows(list(found.values()))
        logger.info(
            "MarketSync traded metadata: refreshed {} of {} traded markets",
            total,
            len(traded_conditions),
        )
        return total
