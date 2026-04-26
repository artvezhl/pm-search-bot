"""Process-wide Telegram notifier.

Exposes a single ``Notifier`` instance per process. It lazily builds a Bot
object from `TELEGRAM_BOT_TOKEN` and posts messages to `TRADER_NOTIFY_CHAT_ID`.
If either is missing, `send()` is a no-op and logs at DEBUG.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from app.config import get_settings
from app.logger import logger

if TYPE_CHECKING:
    from telegram import Bot


class Notifier:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._bot: Bot | None = None
        self._lock = asyncio.Lock()

    def _build_bot(self) -> "Bot | None":
        token = self._settings.telegram_bot_token
        if not token:
            return None
        try:
            from telegram import Bot  # local import: heavy module

            return Bot(token=token)
        except Exception as e:  # noqa: BLE001
            logger.warning("Telegram bot init failed: {}", e)
            return None

    async def send(self, text: str) -> None:
        chat_id = self._settings.trader_notify_chat_id
        if not chat_id:
            logger.debug("Notifier: TRADER_NOTIFY_CHAT_ID unset; skip: {}", text[:80])
            return
        async with self._lock:
            if self._bot is None:
                self._bot = self._build_bot()
            if self._bot is None:
                return
        try:
            await self._bot.send_message(chat_id=int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id, text=text)
        except Exception as e:  # noqa: BLE001
            logger.warning("Notifier send failed: {}", e)


_notifier: Notifier | None = None


def get_notifier() -> Notifier:
    global _notifier
    if _notifier is None:
        _notifier = Notifier()
    return _notifier
