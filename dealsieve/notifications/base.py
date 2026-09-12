from __future__ import annotations

from typing import Protocol

from dealsieve.schemas import Notification


class Notifier(Protocol):
    name: str

    def send(self, notification: Notification) -> str | None:
        """Deliver. Return a delivery reference (e.g. Telegram message id) or None."""
        ...


class RecordingNotifier:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> str | None:
        self.sent.append(notification)
        return f"recorded-{len(self.sent)}"


def get_notifier() -> Notifier:
    """Choose by DEALSIEVE_NOTIFIER: console (default) | telegram. W4 implements."""
    raise NotImplementedError("W4: dealsieve.notifications.base.get_notifier")
