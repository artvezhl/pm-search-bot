"""Ingestion coordinator.

Responsibilities:
    * pull active markets from CLOB REST every `INGESTION_POLL_INTERVAL_SEC`
    * upsert them into `markets`
    * backfill recent trades from CLOB REST (/data/trades) for each market
    * subscribe to CLOB WebSocket for live trades across all tracked assets
    * normalize each trade and persist it into the `trades` hypertable

All ingested trades are kept — no pre-filter on maker_address — so that the
Player Tracker can discover new wallets with large positions automatically.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy import func as sa_func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.db.models import Market, Trade
from app.ingestion.clob_client import CLOBClient, parse_trade_timestamp
from app.ingestion.clob_ws import CLOBWebSocket
from app.logger import logger


def _parse_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"true", "1", "yes", "y", "on"}:
        return True
    if s in {"false", "0", "no", "n", "off", ""}:
        return False
    return default


def _normalize_outcome(raw: Any) -> str:
    s = str(raw or "").strip().upper()
    if s in {"YES", "1", "TRUE"}:
        return "YES"
    if s in {"NO", "0", "FALSE"}:
        return "NO"
    return s or "UNKNOWN"


def _normalize_side(raw: Any) -> str:
    s = str(raw or "").strip().upper()
    if s in {"BUY", "SELL"}:
        return s
    return "BUY"


def normalize_trade(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map a CLOB REST/WS trade payload to our `trades` row shape."""
    market_id = (
        raw.get("market")
        or raw.get("market_id")
        or raw.get("condition_id")
        or raw.get("conditionId")
    )
    maker = (
        raw.get("maker_address")
        or raw.get("maker")
        or raw.get("user")
        or raw.get("owner")
        or raw.get("proxyWallet")
    )

    price = _parse_float(raw.get("price"))
    size = _parse_float(raw.get("size") or raw.get("shares") or raw.get("notional"))
    if size == 0:
        # Some payloads report token amount; as a fallback compute USDC notional.
        size = _parse_float(raw.get("amount")) or 0.0
    side = _normalize_side(raw.get("side") or raw.get("action"))
    outcome = _normalize_outcome(raw.get("outcome") or raw.get("token_outcome"))
    ts = parse_trade_timestamp(
        raw.get("timestamp")
        or raw.get("match_time")
        or raw.get("created_at")
        or raw.get("ts")
    )
    tx_hash = raw.get("tx_hash") or raw.get("transaction_hash") or raw.get("transactionHash")

    trade_id = raw.get("id") or raw.get("trade_id") or raw.get("hash")
    if not trade_id and tx_hash:
        # Data API `/trades` may not include explicit `id`; derive compact deterministic id.
        digest_src = (
            f"{tx_hash}|{raw.get('asset') or raw.get('asset_id') or ''}|"
            f"{side}|{raw.get('price') or ''}|{raw.get('size') or ''}|{ts.timestamp()}"
        )
        trade_id = f"dapi_{hashlib.sha1(digest_src.encode('utf-8')).hexdigest()}"

    if not trade_id or not market_id or not maker:
        return None

    return {
        "trade_id": str(trade_id),
        "market_id": str(market_id).lower(),
        "asset_id": (
            raw.get("asset_id")
            or raw.get("token_id")
            or raw.get("tokenId")
            or raw.get("asset")
        ),
        "maker_address": str(maker).lower(),
        "taker_address": (str(raw.get("taker") or raw.get("taker_address") or "") or None),
        "outcome": outcome,
        "price": Decimal(str(price)),
        "size": Decimal(str(size)),
        "side": side,
        "tx_hash": tx_hash,
        "timestamp": ts,
    }


def _parse_clob_market(raw: dict[str, Any]) -> dict[str, Any]:
    tokens = raw.get("tokens") or []
    token_yes = None
    token_no = None
    for tok in tokens:
        outcome = _normalize_outcome(tok.get("outcome"))
        if outcome == "YES":
            token_yes = tok.get("token_id") or tok.get("tokenId") or tok.get("asset_id")
        elif outcome == "NO":
            token_no = tok.get("token_id") or tok.get("tokenId") or tok.get("asset_id")
    # Some payloads expose direct token_id_* fields even when `tokens` is sparse.
    token_yes = token_yes or raw.get("token_id_yes") or raw.get("tokenIdYes")
    token_no = token_no or raw.get("token_id_no") or raw.get("tokenIdNo")

    end_date_iso = raw.get("end_date_iso") or raw.get("endDate") or raw.get("end_date")
    end_date: datetime | None = None
    if end_date_iso:
        try:
            end_date = datetime.fromisoformat(str(end_date_iso).replace("Z", "+00:00"))
        except ValueError:
            end_date = None

    resolved = _parse_bool(raw.get("archived"), default=False) or _parse_bool(
        raw.get("resolved"), default=False
    )
    now_utc = datetime.now(tz=timezone.utc)
    # Prefer deterministic open/closed state from end_date + resolved flag.
    inferred_closed = resolved or (end_date is not None and end_date <= now_utc)
    inferred_active = not inferred_closed

    return {
        "condition_id": str(raw.get("condition_id") or raw.get("conditionId") or "").lower(),
        "question": str(raw.get("question") or raw.get("description") or "")[:4096],
        "category": raw.get("category"),
        "slug": raw.get("market_slug") or raw.get("slug"),
        "token_id_yes": token_yes,
        "token_id_no": token_no,
        "end_date": end_date,
        "active": inferred_active,
        "closed": inferred_closed,
        "resolved": resolved,
    }


async def persist_trades(session: AsyncSession, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    stmt = pg_insert(Trade).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["trade_id", "timestamp"])
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount or 0


async def upsert_markets(session: AsyncSession, markets: list[dict[str, Any]]) -> int:
    if not markets:
        return 0

    # Postgres has a hard cap on bind params per statement.
    # Upserting tens of thousands of markets at once can exceed it and fail.
    # Keep chunks conservative to remain stable across schema changes.
    chunk_size = 500
    total = 0
    for i in range(0, len(markets), chunk_size):
        chunk = markets[i : i + chunk_size]
        stmt = pg_insert(Market).values(chunk)
        update_cols = {
            c.name: getattr(stmt.excluded, c.name)
            for c in Market.__table__.columns
            if c.name not in {"condition_id"}
        }
        # Never erase known token IDs when source payload omits them.
        update_cols["token_id_yes"] = sa_func.coalesce(
            stmt.excluded.token_id_yes, Market.token_id_yes
        )
        update_cols["token_id_no"] = sa_func.coalesce(
            stmt.excluded.token_id_no, Market.token_id_no
        )
        stmt = stmt.on_conflict_do_update(index_elements=["condition_id"], set_=update_cols)
        result = await session.execute(stmt)
        total += result.rowcount or 0
    await session.commit()
    return total


async def list_tracked_asset_ids(session: AsyncSession) -> list[str]:
    rows = (
        await session.execute(
            select(Market.token_id_yes, Market.token_id_no).where(
                Market.active.is_(True), Market.closed.is_(False)
            )
        )
    ).all()
    ids: list[str] = []
    for yes_id, no_id in rows:
        if yes_id:
            ids.append(yes_id)
        if no_id:
            ids.append(no_id)
    return ids


class IngestionService:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._stop = asyncio.Event()
        self._ws: CLOBWebSocket | None = None
        # Cursor for global `/data/trades` pagination to avoid re-reading stale page 1.
        self._global_trades_cursor: str = ""

    def stop(self) -> None:
        self._stop.set()
        if self._ws:
            self._ws.stop()

    async def on_ws_trade(self, evt: dict[str, Any]) -> None:
        normalized = normalize_trade(evt)
        if not normalized:
            return
        async with AsyncSessionLocal() as session:
            await persist_trades(session, [normalized])

    async def refresh_markets_and_trades(self) -> None:
        """Single pass: list markets from CLOB + backfill recent trades per market."""
        async with CLOBClient() as clob, AsyncSessionLocal() as session:
            try:
                raw_markets = await clob.fetch_active_markets()
            except Exception as e:  # noqa: BLE001
                logger.warning("Ingestion: failed to fetch markets: {}", e)
                return

            parsed = [
                _parse_clob_market(m)
                for m in raw_markets
                if (
                    m.get("condition_id")
                    or m.get("conditionId")
                    or m.get("id")
                    or m.get("market")
                )
            ]
            parsed = [p for p in parsed if p["condition_id"]]
            logger.info(
                "Ingestion: fetched {} raw markets, parsed {}",
                len(raw_markets),
                len(parsed),
            )
            if parsed:
                await upsert_markets(session, parsed)
                logger.info("Ingestion: upserted {} markets", len(parsed))

            total_trades = 0
            total_raw_trades = 0
            total_normalized_trades = 0
            for market in parsed[:100]:  # cap per cycle to avoid rate-limit storms
                try:
                    trades = await clob.fetch_trades(market_id=market["condition_id"], limit=50)
                except Exception as e:  # noqa: BLE001
                    logger.debug("Ingestion: fetch_trades({}) failed: {}", market["condition_id"], e)
                    continue
                total_raw_trades += len(trades)
                normalized_rows: list[dict[str, Any]] = []
                for t in trades:
                    nt = normalize_trade(t)
                    if nt is not None:
                        normalized_rows.append(nt)
                total_normalized_trades += len(normalized_rows)
                if normalized_rows:
                    total_trades += await persist_trades(session, normalized_rows)
            logger.info(
                "Ingestion REST market-scoped: raw={} normalized={} inserted={}",
                total_raw_trades,
                total_normalized_trades,
                total_trades,
            )

            # Fallback: when market-scoped pulls return stale/empty windows, ingest
            # latest global trades to keep `trades` table fresh for observability.
            if total_trades == 0:
                cursor = self._global_trades_cursor
                total_raw_global = 0
                total_normalized_global = 0
                total_inserted_global = 0
                max_pages = 3
                for _ in range(max_pages):
                    try:
                        page, next_cursor = await clob.fetch_trades_page(
                            limit=1000,
                            next_cursor=cursor or None,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning("Ingestion global fallback: fetch_trades failed: {}", e)
                        break
                    if not page:
                        break
                    total_raw_global += len(page)
                    normalized_global = [normalize_trade(t) for t in page]
                    normalized_global = [t for t in normalized_global if t is not None]
                    total_normalized_global += len(normalized_global)
                    if normalized_global:
                        total_inserted_global += await persist_trades(session, normalized_global)
                    if not next_cursor or next_cursor == cursor:
                        break
                    cursor = next_cursor
                self._global_trades_cursor = cursor
                logger.info(
                    "Ingestion REST global fallback: raw={} normalized={} inserted={} cursor={}",
                    total_raw_global,
                    total_normalized_global,
                    total_inserted_global,
                    self._global_trades_cursor or "<empty>",
                )

    async def _run_ws_loop(self) -> None:
        """Live stream of trades. Market/trade REST backfill is Celery's job."""
        async with AsyncSessionLocal() as session:
            asset_ids = (await list_tracked_asset_ids(session))[:500]
        if not asset_ids:
            asset_ids = await self._fallback_asset_ids_from_recent_trades()

        while not self._stop.is_set():
            if not asset_ids:
                logger.info(
                    "Ingestion WS: no tracked assets in markets, trying recent global trades fallback"
                )
                asset_ids = await self._fallback_asset_ids_from_recent_trades()
                if asset_ids:
                    logger.info(
                        "Ingestion WS: fallback produced {} assets, retrying websocket",
                        len(asset_ids),
                    )
                    continue
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=60.0)
                except asyncio.TimeoutError:
                    pass
                async with AsyncSessionLocal() as session:
                    asset_ids = (await list_tracked_asset_ids(session))[:500]
                continue

            self._ws = CLOBWebSocket(asset_ids=asset_ids, on_trade=self.on_ws_trade)
            try:
                await self._ws.run()
            except Exception as e:  # noqa: BLE001
                logger.warning("Ingestion WS closed: {}; will refresh asset list", e)

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            async with AsyncSessionLocal() as session:
                asset_ids = (await list_tracked_asset_ids(session))[:500]
            if not asset_ids:
                asset_ids = await self._fallback_asset_ids_from_recent_trades()

    async def _fallback_asset_ids_from_recent_trades(self) -> list[str]:
        """Use global trades feed to get currently active asset IDs.

        This keeps WS ingestion alive even when market flags in DB are stale.
        """
        try:
            async with CLOBClient() as clob:
                trades = await clob.fetch_trades(limit=1000)
        except Exception as e:  # noqa: BLE001
            logger.warning("Ingestion WS fallback: global trades fetch failed: {}", e)
            return []

        asset_ids: list[str] = []
        seen: set[str] = set()
        for t in trades:
            aid = t.get("asset_id") or t.get("token_id") or t.get("tokenId")
            if not aid:
                continue
            sid = str(aid)
            if sid in seen:
                continue
            seen.add(sid)
            asset_ids.append(sid)
            if len(asset_ids) >= 500:
                break
        logger.info(
            "Ingestion WS fallback: collected {} assets from {} global trades",
            len(asset_ids),
            len(trades),
        )
        return asset_ids

    async def run(self) -> None:
        logger.info("Ingestion: starting WS loop")
        await self._run_ws_loop()
