"""Telegram long-polling bot.

Long-polls ``getUpdates``: plain text becomes an ``InboundMessage`` via ``from_text(channel=TELEGRAM)`` and is
run through the full pipeline; documents are downloaded via ``getFile`` and attached, with PDF text
extracted via ``pypdf``; ``/status`` and ``/deal <n>`` are answered directly from the repo; a
``callback_query`` (from the inline keyboard `dealsieve.notifications.telegram.TelegramNotifier` sends,
draft-specific approve/reject callback data and opportunity-specific ignore/review callback data) routes
through the same audited diligence actions as the API. Exits cleanly on Ctrl-C.
"""

from __future__ import annotations

import hashlib
import io
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from dealsieve.diligence import acknowledge_opportunity, approve_and_send, reject_draft
from dealsieve.ingestion.text import from_text
from dealsieve.notifications import Notifier, get_notifier
from dealsieve.outbound import Outbox, get_outbox
from dealsieve.persistence import Repo
from dealsieve.pipeline import process_inbound
from dealsieve.policy import InvestmentPolicy, load_policy
from dealsieve.schemas import Attachment, Channel

TELEGRAM_API_BASE = "https://api.telegram.org"
POLL_TIMEOUT_S = 30


def _api_url(token: str, method: str) -> str:
    return f"{TELEGRAM_API_BASE}/bot{token}/{method}"


def _extract_pdf_text(data: bytes) -> str | None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return text.strip() or None
    except Exception:
        return None


@dataclass
class TelegramBot:
    """Owns one long-poll loop against a single bot token."""

    token: str
    repo: Repo
    policy: InvestmentPolicy
    notifier: Notifier = field(default_factory=get_notifier)
    outbox: Outbox | None = None
    authorized_chat_id: str | None = None
    client: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=POLL_TIMEOUT_S + 10))

    @classmethod
    def from_env(cls) -> TelegramBot:
        import os

        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not chat_id:
            raise RuntimeError("TELEGRAM_CHAT_ID is not set")
        repo = Repo()
        repo.init_schema()
        policy = load_policy()
        return cls(
            token=token,
            repo=repo,
            policy=policy,
            outbox=get_outbox(policy),
            authorized_chat_id=chat_id,
        )

    # ------------------------------------------------------------------------------- Telegram API helpers

    def _send_text(self, chat_id: int | str, text: str) -> None:
        try:
            self.client.post(_api_url(self.token, "sendMessage"), json={"chat_id": chat_id, "text": text})
        except httpx.HTTPError as exc:
            print(f"[telegram-bot] failed to send message: {exc}")

    def _answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            self.client.post(_api_url(self.token, "answerCallbackQuery"), json=payload)
        except httpx.HTTPError as exc:
            print(f"[telegram-bot] failed to answer callback: {exc}")

    def _download_file(self, file_id: str) -> tuple[bytes, str]:
        resp = self.client.get(_api_url(self.token, "getFile"), params={"file_id": file_id})
        resp.raise_for_status()
        file_path = resp.json()["result"]["file_path"]
        data_resp = self.client.get(f"{TELEGRAM_API_BASE}/file/bot{self.token}/{file_path}")
        data_resp.raise_for_status()
        return data_resp.content, file_path

    # ------------------------------------------------------------------------------- commands

    def _handle_status(self, chat_id: int | str) -> None:
        stats = self.repo.dashboard_stats(self.policy.policy_version)
        text = (
            f"Encountered {stats.encountered} | DEAD {stats.dead}  WATCH {stats.watch}  "
            f"NEAR {stats.near}  REVIEW {stats.review}\n"
            f"Last 7 days: {stats.conditions_changed_7d} conditions changed, "
            f"{stats.threshold_crossings_7d} threshold crossings, "
            f"{stats.human_interruptions_7d} human interruptions"
        )
        self._send_text(chat_id, text)

    def _handle_deal(self, chat_id: int | str, deal_number: str) -> None:
        detail = self.repo.opportunity_detail(deal_number.strip())
        if detail is None:
            self._send_text(chat_id, f"No opportunity for deal #{deal_number}")
            return
        opp = detail.opportunity
        lines = [f"Deal #{opp.deal_number}: {opp.display_name}", f"Status: {opp.status.value}"]
        if opp.current_asking_price is not None:
            lines.append(f"Asking: ${opp.current_asking_price:,.0f}")
        if opp.viability is not None and opp.viability.max_viable_price is not None:
            lines.append(f"Max viable: ${opp.viability.max_viable_price:,.0f}")
        if opp.reason_summary:
            lines.append(opp.reason_summary)
        self._send_text(chat_id, "\n".join(lines))

    # ------------------------------------------------------------------------------- callback_query (buttons)

    def _handle_callback(self, callback_query: dict[str, Any]) -> None:
        callback_id = callback_query["id"]
        data = callback_query.get("data") or ""
        chat = (callback_query.get("message") or {}).get("chat") or {}
        chat_id = chat.get("id")
        action, separator, target_id = data.partition(":")

        configured_chat_id = self.authorized_chat_id
        if configured_chat_id is None:
            import os

            configured_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if chat_id is None or configured_chat_id is None or str(chat_id) != str(configured_chat_id):
            self._answer_callback(callback_id, "This chat is not authorized for DealSieve actions.")
            return
        if not separator or not target_id:
            self._answer_callback(callback_id, "Invalid action.")
            return

        principal = f"human:telegram:{chat_id}"

        try:
            if action == "approve":
                sent = approve_and_send(
                    target_id,
                    repo=self.repo,
                    policy=self.policy,
                    outbox=self.outbox or get_outbox(self.policy),
                    principal=principal,
                )
                self._answer_callback(callback_id, "Approved and sent.")
                self._send_text(chat_id, f"Sent to {sent.to_email or 'the broker'}: {sent.subject}")
            elif action == "reject":
                reject_draft(target_id, repo=self.repo, principal=principal)
                self._answer_callback(callback_id, "Rejected.")
            elif action in ("review", "ignore"):
                # Review/ignore buttons target the opportunity, not a specific notification (see
                # module docstring), so the most recently created notification for it is the best
                # guess at "the alert this button was on" for the decision-memory record below.
                notification = max(
                    self.repo.list_notifications(opportunity_id=target_id),
                    key=lambda n: n.created_at,
                    default=None,
                )
                acknowledge_opportunity(
                    target_id,
                    repo=self.repo,
                    principal=principal,
                    notification=notification,
                    action=action,  # type: ignore[arg-type]
                )
                self._answer_callback(callback_id, "Noted." if action == "ignore" else "Opening review.")
            else:
                self._answer_callback(callback_id)
        except Exception as exc:
            print(f"[telegram-bot] callback error: {exc}")
            self._answer_callback(callback_id, "Sorry, something went wrong handling that.")

    # ------------------------------------------------------------------------------- messages

    def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = message.get("text")
        document = message.get("document")

        if chat_id is None:
            return

        if text and text.startswith("/status"):
            self._handle_status(chat_id)
            return
        if text and text.startswith("/deal"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                self._handle_deal(chat_id, parts[1])
            else:
                self._send_text(chat_id, "Usage: /deal <number>")
            return

        try:
            inbound = self._build_inbound(
                chat_id, text=text, document=document, caption=message.get("caption")
            )
        except Exception as exc:
            self._send_text(chat_id, f"Sorry, I couldn't read that: {exc}")
            return
        if inbound is None:
            return

        try:
            outcome = process_inbound(
                inbound,
                repo=self.repo,
                policy=self.policy,
                notifier=self.notifier,
                outbox=self.outbox,
            )
            self._send_text(chat_id, outcome.summary)
        except Exception as exc:
            self._send_text(chat_id, f"Sorry, I couldn't process that: {exc}")

    def _build_inbound(
        self,
        chat_id: int | str,
        *,
        text: str | None,
        document: dict[str, Any] | None,
        caption: str | None = None,
    ):
        if document is not None:
            data, file_path = self._download_file(document["file_id"])
            filename = document.get("file_name") or file_path.rsplit("/", 1)[-1]
            content_type = document.get("mime_type") or "application/octet-stream"
            attachment_text: str | None = None
            lower_name = filename.lower()
            if content_type == "application/pdf" or lower_name.endswith(".pdf"):
                attachment_text = _extract_pdf_text(data)
            elif content_type.startswith("text/") or lower_name.endswith((".md", ".txt", ".csv")):
                attachment_text = data.decode("utf-8", errors="replace")
            attachment = Attachment(
                filename=filename,
                content_type=content_type,
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                text=attachment_text,
            )
            body_text = caption.strip() if caption and caption.strip() else f"[document: {filename}]"
            inbound = from_text(body_text, channel=Channel.TELEGRAM, sender=str(chat_id))
            return inbound.model_copy(update={"attachments": [attachment]})
        if text:
            return from_text(text, channel=Channel.TELEGRAM, sender=str(chat_id))
        return None

    # ------------------------------------------------------------------------------- main loop

    def run(self) -> None:
        offset: int | None = None
        print("[telegram-bot] long-polling for updates. Press Ctrl-C to stop.")
        try:
            while True:
                params: dict[str, Any] = {"timeout": POLL_TIMEOUT_S}
                if offset is not None:
                    params["offset"] = offset
                try:
                    resp = self.client.get(_api_url(self.token, "getUpdates"), params=params)
                    resp.raise_for_status()
                    updates = resp.json().get("result", [])
                except httpx.HTTPError as exc:
                    print(f"[telegram-bot] network error, retrying: {exc}")
                    time.sleep(3)
                    continue

                for update in updates:
                    offset = update["update_id"] + 1
                    if "callback_query" in update:
                        self._handle_callback(update["callback_query"])
                    elif "message" in update:
                        self._handle_message(update["message"])
        except KeyboardInterrupt:
            print("\n[telegram-bot] stopped.")


def run_bot() -> None:
    TelegramBot.from_env().run()


if __name__ == "__main__":
    run_bot()
