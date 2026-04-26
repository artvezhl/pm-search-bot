"""Real-time subscription to Polymarket CLOB WebSocket (market channel).

Polymarket's market-channel WebSocket delivers `trade` and `book` messages
for a list of `assets_ids` (ERC-1155 conditional token IDs). We subscribe to
every active asset we know about, and push normalized trades into the
ingestion pipeline.

Reference: https://docs.polymarket.com/ websockets (market endpoint).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from app.config import get_settings
from app.logger import logger

TradeHandler = Callable[[dict[str, Any]], Awaitable[None]]


class CLOBWebSocket:
    def __init__(
        self,
        asset_ids: list[str],
        on_trade: TradeHandler,
        url: str | None = None,
    ) -> None:
        self._url = url or get_settings().clob_ws_url
        self._asset_ids = asset_ids
        self._on_trade = on_trade
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        """Main loop. Reconnects indefinitely until `stop()` is called."""
        if not self._asset_ids:
            logger.warning("CLOB WS: no asset IDs given, nothing to subscribe")
            return

        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                await self._run_once()
                backoff = 1.0  # reset after a clean session
            except Exception as e:  # noqa: BLE001
                logger.warning("CLOB WS error: {}; reconnecting in {:.1f}s", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _run_once(self) -> None:
        logger.info(
            "CLOB WS: connecting, subscribing to {} assets", len(self._asset_ids)
        )
        async with websockets.connect(
            self._url, ping_interval=20, ping_timeout=20, close_timeout=10
        ) as ws:
            subscribe_msg = {
                "type": "MARKET",
                "assets_ids": self._asset_ids,
            }
            await ws.send(json.dumps(subscribe_msg))

            while not self._stop_event.is_set():
                raw = await ws.recv()
                await self._handle_raw(raw)

    async def _handle_raw(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        # Server can send either an object or a list of events.
        if isinstance(msg, list):
            for item in msg:
                await self._handle_event(item)
        elif isinstance(msg, dict):
            await self._handle_event(msg)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = (event.get("event_type") or event.get("type") or "").lower()
        if event_type in {"trade", "last_trade_price", "trades"}:
            await self._on_trade(event)
        # `book` events are ignored here — orderbook snapshots are polled via REST
        # when we need them (slippage checks in execution).
