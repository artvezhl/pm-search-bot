"""On-chain listener for Polygon (market resolution events).

Only `ConditionResolution` of the ConditionalTokens contract is needed for MVP
— it tells us the payout vector for a market once resolved, which we use to
compute PnL of bot positions.

The contract address of ConditionalTokens on Polygon is
0x4D97DCd97eC945f40cF65F87097ACe5EA0476045.

If no RPC URL is configured, the listener is silently disabled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from web3 import Web3

from app.config import get_settings
from app.logger import logger

CONDITIONAL_TOKENS_ADDRESS = Web3.to_checksum_address(
    "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
)

# Minimal ABI for ConditionResolution event.
CONDITIONAL_TOKENS_ABI: list[dict[str, Any]] = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"indexed": False, "internalType": "address", "name": "oracle", "type": "address"},
            {"indexed": True, "internalType": "bytes32", "name": "questionId", "type": "bytes32"},
            {"indexed": False, "internalType": "uint256", "name": "outcomeSlotCount", "type": "uint256"},
            {"indexed": False, "internalType": "uint256[]", "name": "payoutNumerators", "type": "uint256[]"},
        ],
        "name": "ConditionResolution",
        "type": "event",
    }
]

ResolutionHandler = Callable[[dict[str, Any]], Awaitable[None]]


class PolygonListener:
    def __init__(self, on_resolution: ResolutionHandler) -> None:
        self._settings = get_settings()
        self._on_resolution = on_resolution
        self._stop = asyncio.Event()
        self._w3: Web3 | None = None

    def stop(self) -> None:
        self._stop.set()

    def _w3_or_none(self) -> Web3 | None:
        url = self._settings.polygon_rpc_url
        if not url:
            return None
        if self._w3 is None:
            self._w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
        return self._w3

    async def run(self) -> None:
        w3 = self._w3_or_none()
        if w3 is None:
            logger.info("PolygonListener disabled: POLYGON_RPC_URL not set")
            return

        contract = w3.eth.contract(address=CONDITIONAL_TOKENS_ADDRESS, abi=CONDITIONAL_TOKENS_ABI)

        try:
            event_filter = contract.events.ConditionResolution.create_filter(fromBlock="latest")
        except Exception as e:  # noqa: BLE001
            logger.warning("PolygonListener: failed to create filter: {}", e)
            return

        logger.info("PolygonListener: watching ConditionResolution on {}", CONDITIONAL_TOKENS_ADDRESS)
        while not self._stop.is_set():
            try:
                for evt in event_filter.get_new_entries():
                    args = evt["args"]
                    payload = {
                        "condition_id": "0x" + args["conditionId"].hex(),
                        "oracle": args["oracle"],
                        "question_id": "0x" + args["questionId"].hex(),
                        "outcome_slot_count": int(args["outcomeSlotCount"]),
                        "payout_numerators": [int(x) for x in args["payoutNumerators"]],
                        "tx_hash": evt["transactionHash"].hex(),
                        "block": evt["blockNumber"],
                    }
                    await self._on_resolution(payload)
            except Exception as e:  # noqa: BLE001
                logger.warning("PolygonListener poll error: {}", e)

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                pass
