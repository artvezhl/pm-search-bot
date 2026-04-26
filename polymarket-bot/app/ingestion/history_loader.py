from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from web3 import Web3
from web3.exceptions import Web3RPCError
from web3.middleware import ExtraDataToPOAMiddleware

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.db.models import PMMarket, PMTrade
from app.ingestion.clob_client import CLOBClient
from app.logger import logger

CTF_EXCHANGE_ADDRESS = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
NEG_RISK_EXCHANGE_ADDRESS = Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a")
ORDER_FILLED_TOPIC = Web3.keccak(
    text="OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
).to_0x_hex()

EXCHANGE_ABI: list[dict[str, Any]] = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "bytes32", "name": "orderHash", "type": "bytes32"},
            {"indexed": True, "internalType": "address", "name": "maker", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "taker", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "makerAssetId", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "takerAssetId", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "makerAmountFilled", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "takerAmountFilled", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "fee", "type": "uint256"},
        ],
        "name": "OrderFilled",
        "type": "event",
    },
]


@dataclass(slots=True)
class TokenMapEntry:
    condition_id: str
    token_outcome: str | None


class HistoryLoader:
    @staticmethod
    def _normalize_token_id(value: Any) -> str | None:
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        try:
            if s.startswith("0x") or s.startswith("0X"):
                return str(int(s, 16))
            return str(int(s))
        except Exception:
            return None

    """Bootstrap loader from on-chain Polygon logs into pm_trades."""

    def __init__(self) -> None:
        self._settings = get_settings()
        rpc_url = self._settings.alchemy_polygon_url or self._settings.polygon_rpc_url
        self._w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self._exchange_contracts = {
            CTF_EXCHANGE_ADDRESS: self._w3.eth.contract(address=CTF_EXCHANGE_ADDRESS, abi=EXCHANGE_ABI),
            NEG_RISK_EXCHANGE_ADDRESS: self._w3.eth.contract(
                address=NEG_RISK_EXCHANGE_ADDRESS,
                abi=EXCHANGE_ABI,
            ),
        }
        self._chunk_size = int(self._settings.onchain_backfill_chunk_size or 2_000)

    @staticmethod
    def _is_pruned_history_error(exc: Exception) -> bool:
        s = str(exc).lower()
        return "history has been pruned" in s or "pruned for this block" in s

    @staticmethod
    def _is_too_large_logs_query_error(exc: Exception) -> bool:
        s = str(exc).lower()
        return (
            "400 client error" in s
            or "query returned more than" in s
            or "response size should not" in s
            or "log response size exceeded" in s
            or "please limit the query to at most" in s
        )

    async def _build_token_map(self, session: AsyncSession) -> dict[str, TokenMapEntry]:
        token_map: dict[str, TokenMapEntry] = {}

        # Primary source: CLOB markets payload has full token_id mapping for YES/NO tokens.
        try:
            async with CLOBClient() as clob:
                cursor = ""
                for _ in range(200):
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
                    for m in data:
                        condition_id = str(m.get("condition_id") or m.get("conditionId") or "").lower()
                        if not condition_id:
                            continue
                        tokens = m.get("tokens") or []
                        for t in tokens:
                            token_id = self._normalize_token_id(
                                t.get("token_id") or t.get("tokenId") or t.get("asset_id") or t.get("assetId")
                            )
                            if not token_id:
                                continue
                            outcome = t.get("outcome")
                            token_map[token_id] = TokenMapEntry(
                                condition_id=condition_id,
                                token_outcome=(str(outcome).upper() if outcome else None),
                            )
                        for raw_token_id in m.get("tokenIds") or []:
                            token_id = self._normalize_token_id(raw_token_id)
                            if not token_id:
                                continue
                            token_map.setdefault(
                                token_id,
                                TokenMapEntry(condition_id=condition_id, token_outcome=None),
                            )
                    cursor = next_cursor
                    if not cursor or cursor == "LTE=":
                        break
        except Exception as e:
            logger.warning("HistoryLoader token map: CLOB fetch failed, fallback to DB map: {}", e)

        # Secondary fallback: DB snapshot.
        if not token_map:
            rows = (
                await session.execute(
                    select(PMMarket.condition_id, PMMarket.asset_id, PMMarket.token_outcome)
                )
            ).all()
            for condition_id, asset_id, token_outcome in rows:
                token_id = self._normalize_token_id(asset_id)
                if not token_id:
                    continue
                token_map[token_id] = TokenMapEntry(
                    condition_id=str(condition_id),
                    token_outcome=(str(token_outcome).upper() if token_outcome else None),
                )
        return token_map

    def _find_block_for_ts(self, target_ts: int) -> int:
        latest = self._w3.eth.block_number
        lo, hi = 1, latest
        ans = latest
        while lo <= hi:
            mid = (lo + hi) // 2
            ts = int(self._w3.eth.get_block(mid)["timestamp"])
            if ts >= target_ts:
                ans = mid
                hi = mid - 1
            else:
                lo = mid + 1
        return ans

    def _decode_chunk(self, from_block: int, to_block: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        logs = self._w3.eth.get_logs(
            {
                "fromBlock": from_block,
                "toBlock": to_block,
                "address": list(self._exchange_contracts.keys()),
                "topics": [ORDER_FILLED_TOPIC],
            }
        )
        for log in logs:
            try:
                addr = Web3.to_checksum_address(log["address"])
                contract = self._exchange_contracts.get(addr)
                if not contract:
                    continue
                evt = contract.events.OrderFilled().process_log(log)
                maker_asset_id = int(evt["args"]["makerAssetId"])
                taker_asset_id = int(evt["args"]["takerAssetId"])
                maker_amount = int(evt["args"]["makerAmountFilled"])
                taker_amount = int(evt["args"]["takerAmountFilled"])
                token_id = taker_asset_id if maker_asset_id == 0 else maker_asset_id
                if token_id == 0:
                    continue
                rows.append(
                    {
                        "tx_hash": evt["transactionHash"].hex(),
                        "log_index": int(evt["logIndex"]),
                        "block_number": int(evt["blockNumber"]),
                        "maker": evt["args"]["maker"].lower(),
                        "taker": evt["args"]["taker"].lower(),
                        "token_id": str(token_id),
                        "maker_asset_id": maker_asset_id,
                        "taker_asset_id": taker_asset_id,
                        "maker_amount_filled": maker_amount,
                        "taker_amount_filled": taker_amount,
                    }
                )
            except Exception:
                continue
        return rows

    async def run(
        self,
        query_id: int | None = None,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> int:
        # Kept for backward compatibility with existing celery command signatures.
        _ = query_id
        if not self._w3.is_connected():
            logger.warning("HistoryLoader skipped: Polygon RPC not connected")
            return 0

        if date_from and date_to:
            start_dt = datetime.fromisoformat(date_from)
            end_dt = datetime.fromisoformat(date_to)
        else:
            end_dt = datetime.now(tz=timezone.utc)
            start_dt = end_dt - timedelta(days=self._settings.pm_history_days)

        if end_dt <= start_dt:
            raise ValueError("date_to must be greater than date_from")

        start_ts = int(start_dt.replace(tzinfo=timezone.utc).timestamp())
        end_ts = int(end_dt.replace(tzinfo=timezone.utc).timestamp())
        from_block = self._find_block_for_ts(start_ts)
        to_block = self._find_block_for_ts(end_ts)

        total_inserted = 0
        processed_ranges = 0
        async with AsyncSessionLocal() as session:
            token_map = await self._build_token_map(session)
            if not token_map:
                logger.warning("HistoryLoader skipped: PM token map is empty (run pm_market_sync first)")
                return 0
            logger.info("HistoryLoader token map loaded: {} token ids", len(token_map))

            block_ts_cache: dict[int, datetime] = {}
            start = from_block
            current_chunk_size = self._chunk_size
            while start <= to_block:
                end = min(start + current_chunk_size - 1, to_block)
                try:
                    logs = await asyncio.to_thread(self._decode_chunk, start, end)
                except Web3RPCError as e:
                    if self._is_pruned_history_error(e):
                        logger.warning("HistoryLoader: pruned chunk {}..{}, skipping", start, end)
                        start = end + 1
                        continue
                    raise
                except Exception as e:
                    if self._is_too_large_logs_query_error(e):
                        if current_chunk_size > 1:
                            current_chunk_size = max(1, current_chunk_size // 2)
                            logger.warning(
                                "HistoryLoader: reducing block chunk size to {} (failed range {}..{})",
                                current_chunk_size,
                                start,
                                end,
                            )
                            continue
                        # If even a single block fails due provider-side limits, skip only that block.
                        logger.warning(
                            "HistoryLoader: skipping problematic block {} due to provider error: {}",
                            start,
                            e,
                        )
                        start += 1
                        continue
                    raise
                processed_ranges += 1
                if not logs:
                    if processed_ranges % 10 == 0:
                        logger.info(
                            "HistoryLoader progress: blocks {}..{} processed_ranges={} inserted_total={} chunk_size={} (no logs in last range)",
                            from_block,
                            to_block,
                            processed_ranges,
                            total_inserted,
                            current_chunk_size,
                        )
                    start = end + 1
                    continue

                dedup: dict[tuple[str, str, str], dict[str, Any]] = {}
                matched_logs = 0
                sample_token_ids: list[str] = []
                for log in logs:
                    if len(sample_token_ids) < 3:
                        sample_token_ids.append(str(log["token_id"]))
                    token_meta = token_map.get(log["token_id"])
                    if token_meta is None:
                        continue
                    matched_logs += 1

                    maker_asset_id = int(log["maker_asset_id"])
                    maker_amount = Decimal(str(log["maker_amount_filled"])) / Decimal("1000000")
                    taker_amount = Decimal(str(log["taker_amount_filled"])) / Decimal("1000000")
                    if maker_amount <= 0 or taker_amount <= 0:
                        continue
                    if maker_asset_id == 0:
                        # Maker buys outcome tokens for collateral.
                        shares = taker_amount
                        amount_usdc = maker_amount
                        action = "BUY"
                    else:
                        # Maker sells outcome tokens for collateral.
                        shares = maker_amount
                        amount_usdc = taker_amount
                        action = "SELL"
                    if shares <= 0 or amount_usdc <= 0:
                        continue

                    if log["block_number"] not in block_ts_cache:
                        block_ts_cache[log["block_number"]] = datetime.fromtimestamp(
                            int(self._w3.eth.get_block(log["block_number"])["timestamp"]),
                            tz=timezone.utc,
                        )
                    price = amount_usdc / shares
                    key = (log["tx_hash"], token_meta.condition_id, log["maker"])
                    current = dedup.get(key)
                    if current is None:
                        dedup[key] = {
                            "tx_hash": log["tx_hash"],
                            "block_time": block_ts_cache[log["block_number"]],
                            "condition_id": token_meta.condition_id,
                            "token_outcome": token_meta.token_outcome,
                            "maker_address": log["maker"],
                            "taker_address": log["taker"],
                            "price": price,
                            "amount_usdc": amount_usdc,
                            "shares": shares,
                            "action": action,
                            "raw_data": log,
                        }
                    else:
                        current["amount_usdc"] += amount_usdc
                        current["shares"] += shares
                        if current["shares"] > 0:
                            current["price"] = current["amount_usdc"] / current["shares"]

                inserted_chunk = 0
                if dedup:
                    inserted_chunk = await self._insert_rows(list(dedup.values()))
                    total_inserted += inserted_chunk
                if matched_logs == 0 or inserted_chunk == 0:
                    missing_samples = [tid for tid in sample_token_ids if tid not in token_map]
                    logger.info(
                        "HistoryLoader range stats: {}..{} raw_logs={} matched_logs={} dedup_rows={} inserted_chunk={} sample_token_ids={} missing_sample_token_ids={}",
                        start,
                        end,
                        len(logs),
                        matched_logs,
                        len(dedup),
                        inserted_chunk,
                        sample_token_ids,
                        missing_samples,
                    )
                if processed_ranges % 10 == 0:
                    logger.info(
                        "HistoryLoader progress: blocks {}..{} processed_ranges={} inserted_total={} chunk_size={}",
                        from_block,
                        to_block,
                        processed_ranges,
                        total_inserted,
                        current_chunk_size,
                    )
                start = end + 1
        logger.info(
            "HistoryLoader: inserted {} onchain rows date_from={} date_to={}",
            total_inserted,
            date_from,
            date_to,
        )
        return total_inserted

    async def run_daily_range(
        self,
        query_id: int | None = None,
        *,
        date_from: str,
        date_to: str,
    ) -> int:
        # Kept for backward compatibility with celery task signature.
        _ = query_id
        start = datetime.fromisoformat(date_from)
        end = datetime.fromisoformat(date_to)
        if end <= start:
            raise ValueError("date_to must be greater than date_from")

        total = 0
        cursor = end
        while cursor > start:
            prev_cursor = max(cursor - timedelta(days=1), start)
            chunk_from = prev_cursor.strftime("%Y-%m-%d %H:%M:%S")
            chunk_to = cursor.strftime("%Y-%m-%d %H:%M:%S")
            inserted = await self.run(None, date_from=chunk_from, date_to=chunk_to)
            logger.info(
                "HistoryLoader daily chunk: [{} .. {}) inserted={}",
                chunk_from,
                chunk_to,
                inserted,
            )
            total += inserted
            cursor = prev_cursor
        return total

    async def _insert_rows(self, rows: list[dict[str, Any]]) -> int:
        # Keep chunks small to avoid PostgreSQL bind parameter limits.
        chunk_size = 2_000
        inserted = 0
        async with AsyncSessionLocal() as session:
            condition_ids = {str(r["condition_id"]) for r in rows if r.get("condition_id")}
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
