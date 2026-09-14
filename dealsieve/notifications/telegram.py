"""Telegram notifier: posts the alert via the Bot API sendMessage call.

Credentials (``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_CHAT_ID``) come from the environment only and are never
logged or included in any exception message.
"""

from __future__ import annotations

import html
import os
from typing import Any, Protocol

import httpx

from dealsieve.schemas import Channel, Notification

TELEGRAM_API_BASE = "https://api.telegram.org"
_MAX_MESSAGE_CHARS = 4_000


class _HttpPoster(Protocol):
    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response: ...



def _render_html(notification: Notification) -> str:
    """Keep figures aligned, then render the actual draft as readable escaped text."""
    body_lines = [
        line
        for line in (notification.body or "").split("\n")
        if not line.strip().startswith("[")
    ]
    draft_at = next(
        (index for index, line in enumerate(body_lines) if line.startswith("Draft to ")),
        None,
    )
    title = f"<b>{html.escape(notification.title)}</b>"

    if draft_at is None:
        body = "\n".join(body_lines).strip()
        if not body:
            return title
        # Plain escaped text keeps Telegram from adding its automatic "Copy code" control.
        # Alert bodies without a draft are short in normal use, but still stay under the limit.
        room = max(0, _MAX_MESSAGE_CHARS - len(title) - 1)
        escaped = html.escape(body)
        if len(escaped) > room:
            body = body[: max(0, room - 4)].rstrip() + "\n..."
            escaped = html.escape(body)
            while len(escaped) > room and body:
                body = body[:-5].rstrip() + "\n..."
                escaped = html.escape(body)
        return f"{title}\n{escaped}"

    metrics = "\n".join(body_lines[:draft_at]).strip()
    draft_lines = body_lines[draft_at:]
    draft_to = draft_lines[0]
    subject = draft_lines[1] if len(draft_lines) > 1 else ""
    draft_body = "\n".join(draft_lines[3:] if len(draft_lines) > 2 and draft_lines[2] == "" else draft_lines[2:])

    def render(candidate_body: str) -> str:
        parts = [title]
        if metrics:
            parts.append(html.escape(metrics))
        parts.append(f"<b>{html.escape(draft_to)}</b>")
        if subject:
            parts.append(f"<b>{html.escape(subject)}</b>")
        if candidate_body:
            parts.append(html.escape(candidate_body))
        return "\n".join(parts)

    rendered = render(draft_body)
    if len(rendered) <= _MAX_MESSAGE_CHARS:
        return rendered

    lo, hi = 0, len(draft_body)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = draft_body[:mid].rstrip() + "\n..."
        if len(render(candidate)) <= _MAX_MESSAGE_CHARS:
            lo = mid
        else:
            hi = mid - 1
    return render(draft_body[:lo].rstrip() + "\n...")

class TelegramNotifier:
    """Sends the alert to a single chat, with an inline keyboard built from ``notification.actions``.

    Review/ignore buttons target the opportunity; approve/reject buttons target the exact draft
    supplied by the alert formatter.
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
        """Inline keyboard: the primary action (approve) gets its own full-width row so its label is never
        truncated on a phone; the remaining actions share one row beneath it."""
        if not notification.actions:
            return None
        primary = [a for a in notification.actions if a.action == "approve"]
        others = [a for a in notification.actions if a.action != "approve"]
        rows: list[list[dict[str, str]]] = []
        for group in (primary, others):
            if group:
                rows.append(
                    [{"text": a.label, "callback_data": self._callback_data(notification, a.action)} for a in group]
                )
        return {"inline_keyboard": rows}

    @staticmethod
    def _callback_data(notification: Notification, action: str) -> str:
        if action in {"approve", "reject"}:
            draft_id = getattr(notification, "_telegram_draft_id", None)
            if draft_id is None:
                raise ValueError(f"Telegram {action} action is missing its draft id")
            return f"{action}:{draft_id}"
        return f"{action}:{notification.opportunity_id}"

    def send(self, notification: Notification) -> str | None:
        text = _render_html(notification)
        payload: dict[str, Any] = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}
        keyboard = self._keyboard(notification)
        if keyboard is not None:
            payload["reply_markup"] = keyboard

        url = f"{TELEGRAM_API_BASE}/bot{self._token}/sendMessage"
        response = self._client.post(url, json=payload, timeout=self._timeout)
        response.raise_for_status()
        data = response.json()
        message_id = data.get("result", {}).get("message_id")
        return str(message_id) if message_id is not None else None
