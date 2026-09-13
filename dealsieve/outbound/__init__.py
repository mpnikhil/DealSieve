"""Broker-message delivery backends.

The outbox only transports an already-gated :class:`OutboundDraft`.  Approval and
policy screening live in :mod:`dealsieve.diligence`; keeping those decisions out
of delivery backends prevents a mail implementation from bypassing them.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from email.utils import format_datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from dealsieve.policy import InvestmentPolicy, load_policy
from dealsieve.schemas import OutboundDraft, now_utc


class OutboxError(RuntimeError):
    """Raised when an outbound transport cannot deliver a message."""


class Outbox(Protocol):
    name: str

    def send(self, message: OutboundDraft) -> str:
        """Deliver an approved/gated message and return a stable delivery reference."""
        ...


class RecordingOutbox:
    """In-memory outbox used by tests and the offline demo."""

    name = "recording"

    def __init__(self) -> None:
        self.sent: list[OutboundDraft] = []

    def send(self, message: OutboundDraft) -> str:
        self.sent.append(message)
        return f"recorded-{len(self.sent)}"


def _email_message(draft: OutboundDraft, policy: InvestmentPolicy) -> EmailMessage:
    if not draft.to_email:
        raise OutboxError("outbound draft has no recipient email")
    email = EmailMessage()
    email["From"] = f"{policy.outreach.from_name} <{policy.outreach.from_email}>"
    email["To"] = draft.to_email
    email["Subject"] = draft.subject
    email["Date"] = format_datetime(now_utc())
    # Retries of the same persisted draft must present the same transport identity. SMTP
    # providers can then deduplicate a retry rather than treating it as a distinct email.
    digest = sha256(draft.draft_id.encode("utf-8")).hexdigest()
    email["Message-ID"] = f"<dealsieve.{digest}@local>"
    if draft.in_reply_to_message_id:
        reference = draft.in_reply_to_message_id.strip()
        email["In-Reply-To"] = reference
        email["References"] = reference
    email.set_content(draft.body)
    return email


class FileOutbox:
    """Write valid RFC 822 messages to a local directory for safe review."""

    name = "file"

    def __init__(
        self,
        policy: InvestmentPolicy | None = None,
        directory: str | Path | None = None,
    ) -> None:
        self.policy = policy or load_policy()
        self.directory = Path(directory or os.environ.get("DEALSIEVE_OUTBOX_DIR", "data/outbox"))

    def send(self, message: OutboundDraft) -> str:
        email = _email_message(message, self.policy)
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = now_utc().strftime("%Y%m%dT%H%M%S%fZ")
        path = self.directory / f"{stamp}_{message.draft_id}.eml"
        try:
            path.write_bytes(email.as_bytes())
        except OSError as exc:
            raise OutboxError(f"could not write outbound message: {exc}") from exc
        print(f"-> sent to {message.to_email}: {message.subject}")
        return str(path)


class SmtpOutbox:
    """SMTP transport configured exclusively through environment variables."""

    name = "smtp"

    def __init__(self, policy: InvestmentPolicy | None = None) -> None:
        self.policy = policy or load_policy()

    def send(self, message: OutboundDraft) -> str:
        host = os.environ.get("SMTP_HOST")
        if not host:
            raise OutboxError("SMTP_HOST is required for the smtp outbox")
        port = int(os.environ.get("SMTP_PORT", "587"))
        user = os.environ.get("SMTP_USER")
        password = os.environ.get("SMTP_PASSWORD")
        starttls = os.environ.get("SMTP_STARTTLS", "1").strip().lower() not in {"0", "false", "no"}
        email = _email_message(message, self.policy)
        try:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                if starttls:
                    smtp.starttls()
                if user:
                    smtp.login(user, password or "")
                smtp.send_message(email)
        except (OSError, smtplib.SMTPException) as exc:
            raise OutboxError(f"SMTP delivery failed: {exc}") from exc
        return email.get("Message-ID") or f"smtp:{message.draft_id}"


def get_outbox(policy: InvestmentPolicy | None = None) -> Outbox:
    """Choose ``file`` (default), ``smtp`` or ``recording`` from the environment."""
    backend = os.environ.get("DEALSIEVE_OUTBOX", "file").strip().lower()
    if backend == "recording":
        return RecordingOutbox()
    if backend == "smtp":
        return SmtpOutbox(policy)
    if backend == "file":
        return FileOutbox(policy)
    raise OutboxError(f"unknown DEALSIEVE_OUTBOX backend: {backend!r}")


__all__ = [
    "FileOutbox",
    "Outbox",
    "OutboxError",
    "RecordingOutbox",
    "SmtpOutbox",
    "get_outbox",
]
