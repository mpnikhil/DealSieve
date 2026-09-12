"""Plain text / URL pastes (Telegram, manual) into InboundMessage. W2 implements."""

from __future__ import annotations

import hashlib
import re

from dealsieve.schemas import Channel, InboundMessage

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def from_text(
    text: str, *, channel: Channel = Channel.MANUAL, sender: str | None = None, message_id: str | None = None
) -> InboundMessage:
    mid = message_id or f"txt_{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"
    urls = _URL_RE.findall(text or "")
    return InboundMessage(
        message_id=mid,
        channel=channel,
        sender=sender,
        body_text=text,
        urls=urls,
        thread_id=mid,
    )
