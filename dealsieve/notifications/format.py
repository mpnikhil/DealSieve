"""Deterministic alert formatting. W4 implements. Plain text, Telegram-safe (no markdown tables)."""

from __future__ import annotations

from dealsieve.schemas import Channel, Notification, Opportunity, SkepticReport, UnderwritingResult


def format_threshold_alert(
    opportunity: Opportunity,
    previous_run: UnderwritingResult | None,
    new_run: UnderwritingResult,
    skeptic: SkepticReport | None,
    *,
    channel: Channel = Channel.TELEGRAM,
) -> Notification:
    raise NotImplementedError("W4: dealsieve.notifications.format.format_threshold_alert")
