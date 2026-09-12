from __future__ import annotations

import os
from typing import Protocol

from dealsieve.schemas import Channel, Notification


class Notifier(Protocol):
    name: str
    channel: Channel
    """Which Channel this notifier actually delivers over -- callers building the Notification (e.g.
    agents/tools.py's notify_human) should pass this as format_threshold_alert(..., channel=notifier.channel)
    so the stored record reflects where the alert really went, rather than hardcoding a channel."""

    def send(self, notification: Notification) -> str | None:
        """Deliver. Return a delivery reference (e.g. Telegram message id) or None."""
        ...


class RecordingNotifier:
    name = "recording"
    channel = Channel.MANUAL

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> str | None:
        self.sent.append(notification)
        return f"recorded-{len(self.sent)}"


def get_notifier() -> Notifier:
    """Choose by DEALSIEVE_NOTIFIER: console (default) | telegram.

    Telegram is only selected when DEALSIEVE_NOTIFIER=telegram AND both TELEGRAM_BOT_TOKEN and
    TELEGRAM_CHAT_ID are set; otherwise this falls back to the console notifier so the demo always works
    offline.
    """
    backend = os.environ.get("DEALSIEVE_NOTIFIER", "console").strip().lower()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if backend == "telegram" and token and chat_id:
        from dealsieve.notifications.telegram import TelegramNotifier

        return TelegramNotifier(token=token, chat_id=chat_id)

    from dealsieve.notifications.console import ConsoleNotifier

    return ConsoleNotifier()
