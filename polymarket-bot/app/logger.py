"""Centralised loguru configuration.

Importing this module configures loguru's default sink with the level from
settings. All subsequent `from loguru import logger` calls reuse it.
"""

from __future__ import annotations

import sys

from loguru import logger

from app.config import get_settings

_configured = False


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    logger.remove()
    logger.add(
        sys.stdout,
        level=get_settings().log_level.upper(),
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
            "<level>{level: <8}</level> "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
        ),
        enqueue=True,
    )
    _configured = True


configure_logging()

__all__ = ["logger", "configure_logging"]
