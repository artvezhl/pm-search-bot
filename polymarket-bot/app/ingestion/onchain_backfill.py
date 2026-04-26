"""Backfill inferred trade events from Polygon for last N days.

This module scans Conditional Tokens ERC-1155 transfers and maps token IDs to
known Polymarket markets (`markets.token_id_yes/no`). For each matched transfer
we write an inferred BUY trade into `trades`.

Notes:
- On-chain transfers are not a perfect 1:1 representation of CLOB matches.
- We store them as inferred trades to provide historical wallet activity when
  CLOB history endpoints are limited.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from web3 import Web3
from web3.exceptions import Web3RPCError
from web3.middleware import ExtraDataToPOAMiddleware

from app.db import AsyncSessionLocal
from app.db.models import Market
from app.ingestion.ingestion_service import persist_trades
from app.logger import logger

CONDITIONAL_TOKENS_ADDRESS = Web3.to_checksum_address(
    "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
)

ERC1155_ABI: list[dict[str, Any]] = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "operator", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "from", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "to", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "id", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "value", "type": "uint256"},
        ],
        "name": "TransferSingle",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "operator", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "from", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "to", "type": "address"},
            {"indexed": False, "internalType": "uint256[]", "name": "ids", "type": "uint256[]"},
            {"indexed": False, "internalType": "uint256[]", "name": "values", "type": "uint256[]"},
        ],
        "name": "TransferBatch",
        "type": "event",
    },
]

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


@dataclass(slots=True)
class TokenMapEntry:
    condition_id: str
    outcome: str  # YES / NO


class OnchainTradeBackfiller:
    def __init__(self, rpc_url: str, lookback_days: int = 30, chunk_size: int = 2_000) -> None:
        self._w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        # Polygon Bor is PoA-like; without this middleware web3 can raise:
        # ExtraDataLengthError: extraData is 97 bytes, should be 32.
        self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self._lookback_days = lookback_days
        self._chunk_size = chunk_size
        self._contract = self._w3.eth.contract(
            address=CONDITIONAL_TOKENS_ADDRESS, abi=ERC1155_ABI
        )

    @staticmethod
    def _is_pruned_history_error(exc: Exception) -> bool:
        s = str(exc).lower()
        return "history has been pruned" in s or "pruned for this block" in s

    async def _build_token_map(self, session: AsyncSession) -> dict[str, TokenMapEntry]:
        rows = (
            await session.execute(
                select(Market.condition_id, Market.token_id_yes, Market.token_id_no)
            )
        ).all()
        out: dict[str, TokenMapEntry] = {}
        for condition_id, token_yes, token_no in rows:
            if token_yes:
                out[str(token_yes)] = TokenMapEntry(condition_id=condition_id, outcome="YES")
            if token_no:
                out[str(token_no)] = TokenMapEntry(condition_id=condition_id, outcome="NO")
        return out

    def _find_block_for_ts(self, target_ts: int) -> int:
        latest = self._w3.eth.block_number
        lo = 1
        hi = latest
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
        single_evt = self._contract.events.TransferSingle()
        batch_evt = self._contract.events.TransferBatch()

        rows: list[dict[str, Any]] = []
        for log in self._w3.eth.get_logs(
            {"fromBlock": from_block, "toBlock": to_block, "address": CONDITIONAL_TOKENS_ADDRESS}
        ):
            try:
                try:
                    evt = single_evt.process_log(log)
                    rows.append(
                        {
                            "tx_hash": evt["transactionHash"].hex(),
                            "log_index": int(evt["logIndex"]),
                            "block_number": int(evt["blockNumber"]),
                            "from": evt["args"]["from"].lower(),
                            "to": evt["args"]["to"].lower(),
                            "token_id": str(int(evt["args"]["id"])),
                            "value": int(evt["args"]["value"]),
                        }
                    )
                    continue
                except Exception:
                    pass

                evt = batch_evt.process_log(log)
                ids = [str(int(x)) for x in evt["args"]["ids"]]
                vals = [int(x) for x in evt["args"]["values"]]
                for i, token_id in enumerate(ids):
                    rows.append(
                        {
                            "tx_hash": evt["transactionHash"].hex(),
                            "log_index": int(evt["logIndex"]) * 10_000 + i,
                            "block_number": int(evt["blockNumber"]),
                            "from": evt["args"]["from"].lower(),
                            "to": evt["args"]["to"].lower(),
                            "token_id": token_id,
                            "value": vals[i] if i < len(vals) else 0,
                        }
                    )
            except Exception:
                continue
        return rows

    async def run(self) -> int:
        if not self._w3.is_connected():
            logger.warning("Onchain backfill: RPC not connected")
            return 0

        from_ts = int((datetime.now(tz=timezone.utc) - timedelta(days=self._lookback_days)).timestamp())
        from_block = self._find_block_for_ts(from_ts)
        to_block = self._w3.eth.block_number

        logger.info(
            "Onchain backfill: scanning blocks {}..{} ({} days)",
            from_block,
            to_block,
            self._lookback_days,
        )

        inserted_total = 0
        async with AsyncSessionLocal() as session:
            token_map = await self._build_token_map(session)
            if not token_map:
                logger.warning("Onchain backfill: token map is empty, skipping")
                return 0

            for start in range(from_block, to_block + 1, self._chunk_size):
                end = min(start + self._chunk_size - 1, to_block)
                try:
                    rows = await asyncio.to_thread(self._decode_chunk, start, end)
                except Web3RPCError as e:
                    if self._is_pruned_history_error(e):
                        logger.warning(
                            "Onchain backfill: pruned history for blocks {}..{}, skipping chunk",
                            start,
                            end,
                        )
                        continue
                    raise
                if not rows:
                    continue
                block_ts_cache: dict[int, datetime] = {}

                trade_rows: list[dict[str, Any]] = []
                for r in rows:
                    # Skip mint/burn transfer legs.
                    if r["from"] == ZERO_ADDRESS or r["to"] == ZERO_ADDRESS:
                        continue
                    token_meta = token_map.get(r["token_id"])
                    if token_meta is None:
                        continue

                    # Token amounts on-chain are 1e6 scaled for CTF tokens.
                    token_amount = Decimal(str(r["value"])) / Decimal("1000000")
                    if token_amount <= 0:
                        continue

                    # Inferred historical price is not available on-chain from transfer alone.
                    # Use neutral fallback 0.5; notional = token_amount * 0.5.
                    price = Decimal("0.5")
                    size_usd = token_amount * price
                    if r["block_number"] not in block_ts_cache:
                        block_ts_cache[r["block_number"]] = datetime.fromtimestamp(
                            int(self._w3.eth.get_block(r["block_number"])["timestamp"]),
                            tz=timezone.utc,
                        )
                    ts = block_ts_cache[r["block_number"]]
                    trade_rows.append(
                        {
                            "trade_id": f"{r['tx_hash']}:{r['log_index']}:{r['token_id']}",
                            "timestamp": ts,
                            "market_id": token_meta.condition_id,
                            "asset_id": r["token_id"],
                            "maker_address": r["to"],
                            "taker_address": r["from"],
                            "outcome": token_meta.outcome,
                            "price": price,
                            "size": size_usd,
                            "side": "BUY",
                            "tx_hash": r["tx_hash"],
                        }
                    )

                if trade_rows:
                    inserted_total += await persist_trades(session, trade_rows)
                    logger.info(
                        "Onchain backfill: blocks {}..{} -> inserted {} trades",
                        start,
                        end,
                        len(trade_rows),
                    )

        logger.info("Onchain backfill done: inserted {} inferred trades", inserted_total)
        return inserted_total

