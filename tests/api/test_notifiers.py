"""get_notifier() selection by environment, and TelegramNotifier request shaping.

No network calls: TelegramNotifier is exercised with a fake httpx-shaped client.
"""

from __future__ import annotations

from typing import Any

import pytest

from dealsieve.notifications.base import get_notifier
from dealsieve.notifications.console import ConsoleNotifier
from dealsieve.notifications.telegram import TelegramNotifier
from dealsieve.schemas import Channel, Notification, NotificationAction


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
    assert payload["text"] == f"{notification.title}\n\n{notification.body}"

    keyboard = payload["reply_markup"]["inline_keyboard"]
    assert len(keyboard) == 1
    buttons = keyboard[0]
    assert [b["text"] for b in buttons] == ["Review", "Draft broker questions", "Ignore"]
    assert [b["callback_data"] for b in buttons] == [
        "review:opp_123",
        "draft_questions:opp_123",
        "ignore:opp_123",
    ]


def test_telegram_notifier_omits_keyboard_when_no_actions() -> None:
    fake_client = _FakeClient({"result": {"message_id": 1}})
    notifier = TelegramNotifier(token="tok", chat_id="chat", client=fake_client)

    notifier.send(_notification(actions=[]))

    payload = fake_client.calls[0]["json"]
    assert "reply_markup" not in payload
