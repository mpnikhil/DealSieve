"""Plain text / URL pastes (Telegram, manual) into InboundMessage. W2 implements."""

from __future__ import annotations

from dealsieve.schemas import Channel, InboundMessage


def from_text(text: str, *, channel: Channel = Channel.MANUAL, sender: str | None = None, message_id: str | None = None) -> InboundMessage:
    raise NotImplementedError("W2: dealsieve.ingestion.text.from_text")
