"""get_notifier() selection by environment, and TelegramNotifier request shaping.

No network calls: TelegramNotifier is exercised with a fake httpx-shaped client.
"""

from __future__ import annotations

from typing import Any

import pytest

from dealsieve.ingestion.telegram import TelegramBot
from dealsieve.notifications.base import get_notifier
from dealsieve.notifications.console import ConsoleNotifier
from dealsieve.notifications.telegram import TelegramNotifier
from dealsieve.outbound import RecordingOutbox
from dealsieve.schemas import (
    Channel,
    DiligenceRequest,
    EventType,
    Notification,
    NotificationAction,
    Opportunity,
    OutboundDraft,
    Property,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEALSIEVE_NOTIFIER", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


def test_get_notifier_defaults_to_console() -> None:
    assert isinstance(get_notifier(), ConsoleNotifier)


def test_get_notifier_telegram_requires_both_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEALSIEVE_NOTIFIER", "telegram")
    # Neither credential set.
    assert isinstance(get_notifier(), ConsoleNotifier)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    # Token only, no chat id.
    assert isinstance(get_notifier(), ConsoleNotifier)

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    # Both present now.
    assert isinstance(get_notifier(), TelegramNotifier)


def test_get_notifier_telegram_env_but_backend_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    # DEALSIEVE_NOTIFIER not set to "telegram" -> still console.
    assert isinstance(get_notifier(), ConsoleNotifier)


def test_get_notifier_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEALSIEVE_NOTIFIER", "TELEGRAM")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    assert isinstance(get_notifier(), TelegramNotifier)


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.raised = False

    def raise_for_status(self) -> None:
        self.raised = True

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return _FakeResponse(self._payload)


def _notification(**overrides: Any) -> Notification:
    defaults: dict[str, Any] = dict(
        opportunity_id="opp_123",
        kind="threshold_crossed",
        channel=Channel.TELEGRAM,
        title="DEAL #101 JUST BECAME INVESTABLE",
        body="line one\nline two",
        actions=[
            NotificationAction(label="Review", action="review"),
            NotificationAction(label="Draft broker questions", action="draft_questions"),
            NotificationAction(label="Ignore", action="ignore"),
        ],
    )
    defaults.update(overrides)
    return Notification(**defaults)


def test_telegram_notifier_requires_credentials() -> None:
    with pytest.raises(RuntimeError):
        TelegramNotifier(token=None, chat_id=None)


def test_telegram_notifier_never_logs_or_returns_token(capsys: pytest.CaptureFixture[str]) -> None:
    fake_client = _FakeClient({"result": {"message_id": 555}})
    notifier = TelegramNotifier(token="super-secret-token", chat_id="chat-1", client=fake_client)

    result = notifier.send(_notification())

    assert result == "555"
    captured = capsys.readouterr()
    assert "super-secret-token" not in captured.out
    assert "super-secret-token" not in captured.err


def test_telegram_notifier_request_shape() -> None:
    fake_client = _FakeClient({"result": {"message_id": 42}})
    notifier = TelegramNotifier(token="tok123", chat_id="chat-9", client=fake_client)

    notification = _notification()
    message_id = notifier.send(notification)

    assert message_id == "42"
    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]

    assert call["url"] == "https://api.telegram.org/bottok123/sendMessage"
    payload = call["json"]
    assert payload["chat_id"] == "chat-9"
    assert payload["parse_mode"] == "HTML"
    assert payload["text"] == (
        "<b>DEAL #101 JUST BECAME INVESTABLE</b>\n"
        "line one\nline two"
    )

    keyboard = payload["reply_markup"]["inline_keyboard"]
    assert len(keyboard) == 1
    buttons = keyboard[0]
    assert [b["text"] for b in buttons] == ["Review", "Draft broker questions", "Ignore"]
    assert [b["callback_data"] for b in buttons] == [
        "review:opp_123",
        "draft_questions:opp_123",
        "ignore:opp_123",
    ]


def test_telegram_notifier_uses_draft_ids_for_approve_and_reject() -> None:
    fake_client = _FakeClient({"result": {"message_id": 42}})
    notifier = TelegramNotifier(token="tok", chat_id="chat", client=fake_client)
    notification = _notification(
        actions=[
            NotificationAction(label="Approve", action="approve"),
            NotificationAction(label="Reject", action="reject"),
            NotificationAction(label="Ignore", action="ignore"),
            NotificationAction(label="Review", action="review"),
        ]
    )
    object.__setattr__(notification, "_telegram_draft_id", "drf_abc123")

    notifier.send(notification)

    rows = fake_client.calls[0]["json"]["reply_markup"]["inline_keyboard"]
    assert [[button["callback_data"] for button in row] for row in rows] == [
        ["approve:drf_abc123"],
        [
        "reject:drf_abc123",
        "ignore:opp_123",
        "review:opp_123",
        ],
    ]


def test_telegram_notifier_renders_draft_outside_monospace_block() -> None:
    fake_client = _FakeClient({"result": {"message_id": 42}})
    notifier = TelegramNotifier(token="tok", chat_id="chat", client=fake_client)
    notification = _notification(
        body=(
            "Power Inn\n\nPrice            $1,550,000 -> $1,250,000\n\n"
            "Draft to maya@example.com\n"
            "Subject: Diligence questions: Power Inn\n\n"
            "Hi Maya,\n\nCould you send the roof report?"
        ),
        actions=[
            NotificationAction(label="Approve and send", action="approve"),
            NotificationAction(label="Reject", action="reject"),
            NotificationAction(label="Mark for review", action="review"),
        ],
    )
    object.__setattr__(notification, "_telegram_draft_id", "drf_abc123")

    notifier.send(notification)

    payload = fake_client.calls[0]["json"]
    assert payload["text"] == (
        "<b>DEAL #101 JUST BECAME INVESTABLE</b>\n"
        "Power Inn\n\nPrice            $1,550,000 -&gt; $1,250,000\n"
        "<b>Draft to maya@example.com</b>\n"
        "<b>Subject: Diligence questions: Power Inn</b>\n"
        "Hi Maya,\n\nCould you send the roof report?"
    )
    assert payload["parse_mode"] == "HTML"
    assert [[button["text"] for button in row] for row in payload["reply_markup"]["inline_keyboard"]] == [
        ["Approve and send"],
        ["Reject", "Mark for review"],
    ]


def test_telegram_notifier_omits_keyboard_when_no_actions() -> None:
    fake_client = _FakeClient({"result": {"message_id": 1}})
    notifier = TelegramNotifier(token="tok", chat_id="chat", client=fake_client)

    notifier.send(_notification(actions=[]))

    payload = fake_client.calls[0]["json"]
    assert "reply_markup" not in payload


class _BotClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, json: dict[str, Any], **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, **kwargs})
        return _FakeResponse({"ok": True, "result": {"message_id": 1}})


def _telegram_opportunity(repo) -> Opportunity:
    prop = repo.upsert_property(
        Property(
            canonical_address="1 Telegram St, Sacramento, CA",
            normalized_address="1 TELEGRAM ST SACRAMENTO CA",
        )
    )
    return repo.create_opportunity(
        Opportunity(
            property_id=prop.property_id,
            display_name="Telegram Property",
            broker_email="broker@example.com",
            human_attention_required=True,
        )
    )


def test_telegram_callback_authorizes_chat_targets_exact_draft_and_sends(repo, policy) -> None:
    opp = _telegram_opportunity(repo)
    request = DiligenceRequest(
        opportunity_id=opp.opportunity_id,
        topic="Roof",
        question="Roof age?",
    )
    repo.store_diligence_request(request)
    draft = OutboundDraft(
        opportunity_id=opp.opportunity_id,
        to_email=opp.broker_email,
        subject="Diligence questions: Telegram Property",
        body="Roof age?",
        request_ids=[request.request_id],
    )
    repo.store_draft(draft)
    client = _BotClient()
    outbox = RecordingOutbox()
    bot = TelegramBot(
        token="tok",
        repo=repo,
        policy=policy,
        outbox=outbox,
        authorized_chat_id="123",
        client=client,  # type: ignore[arg-type]
    )

    bot._handle_callback(
        {
            "id": "cb1",
            "data": f"approve:{draft.draft_id}",
            "message": {"chat": {"id": 123}},
        }
    )

    assert repo.get_draft(draft.draft_id).status == "sent"
    assert len(outbox.sent) == 1
    assert repo.get_opportunity(opp.opportunity_id).human_attention_required is False
    approved = next(
        event
        for event in repo.list_events(opp.opportunity_id)
        if event.type == EventType.HUMAN_APPROVED_DRAFT
    )
    assert approved.payload["principal"] == "human:telegram:123"


def test_telegram_callback_rejects_wrong_chat_without_mutation(repo, policy) -> None:
    opp = _telegram_opportunity(repo)
    draft = OutboundDraft(
        opportunity_id=opp.opportunity_id,
        to_email=opp.broker_email,
        subject="Diligence questions: Telegram Property",
        body="Roof age?",
    )
    repo.store_draft(draft)
    bot = TelegramBot(
        token="tok",
        repo=repo,
        policy=policy,
        outbox=RecordingOutbox(),
        authorized_chat_id="123",
        client=_BotClient(),  # type: ignore[arg-type]
    )

    bot._handle_callback(
        {
            "id": "cb2",
            "data": f"reject:{draft.draft_id}",
            "message": {"chat": {"id": 999}},
        }
    )

    assert repo.get_draft(draft.draft_id).status == "pending"
    assert repo.list_events(opp.opportunity_id) == []
