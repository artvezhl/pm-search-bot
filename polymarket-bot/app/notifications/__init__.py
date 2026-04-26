"""Notifications layer — Telegram bot + notifier singleton."""

from app.notifications.notifier import Notifier, get_notifier

__all__ = ["Notifier", "get_notifier"]
