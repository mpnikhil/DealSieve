"""Telegram notifier: posts the alert via the Bot API sendMessage call.

Credentials (``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_CHAT_ID``) come from the environment only and are never
logged or included in any exception message.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

import httpx

from dealsieve.schemas import Channel, Notification

TELEGRAM_API_BASE = "https://api.telegram.org"


class _HttpPoster(Protocol):
    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response: ...


class TelegramNotifier:
    """Sends the alert to a single chat, with an inline keyboard built from ``notification.actions``.

    ``callback_data`` for each button is ``"<action>:<opportunity_id>"`` (e.g. "review:opp_abc123") so the
    long-polling bot (dealsieve/ingestion/telegram.py) can dispatch the callback_query without any extra
    lookup.
    """

    name = "telegram"
    channel = Channel.TELEGRAM

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        *,
        client: _HttpPoster | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self._chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        if not self._token or not self._chat_id:
            raise RuntimeError("TelegramNotifier requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        # A caller-supplied client (or a test double) is used verbatim; otherwise the stateless
        # ``httpx`` module functions are used so no connection is held open between sends.
        self._client: _HttpPoster = client if client is not None else httpx  # type: ignore[assignment]
        self._timeout = timeout

    def _keyboard(self, notification: Notification) -> dict[str, Any] | None:
        if not notification.actions:
            return None
        return {
            "inline_keyboard": [
                [
                    {
                        "text": action.label,
                        "callback_data": f"{action.action}:{notification.opportunity_id}",
                    }
                    for action in notification.actions
                ]
            ]
        }

    def send(self, notification: Notification) -> str | None:
        text = f"{notification.title}\n\n{notification.body}" if notification.body else notification.title
        payload: dict[str, Any] = {"chat_id": self._chat_id, "text": text}
        keyboard = self._keyboard(notification)
        if keyboard is not None:
            payload["reply_markup"] = keyboard

        url = f"{TELEGRAM_API_BASE}/bot{self._token}/sendMessage"
        response = self._client.post(url, json=payload, timeout=self._timeout)
        response.raise_for_status()
        data = response.json()
        message_id = data.get("result", {}).get("message_id")
        return str(message_id) if message_id is not None else None
