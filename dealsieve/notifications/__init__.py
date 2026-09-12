"""Human interruption channel. Owned by W4.

Notifier.send(notification) -> delivery_ref | None. Implementations: ConsoleNotifier (default, prints),
TelegramNotifier (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID), RecordingNotifier (tests; keeps a list).
format_threshold_alert(...) builds the canonical "DEAL #184 JUST BECAME INVESTABLE" message deterministically.
"""

from dealsieve.notifications.base import Notifier, RecordingNotifier, get_notifier  # noqa: F401
from dealsieve.notifications.format import (  # noqa: F401
    format_fell_below_alert,
    format_stalled_alert,
    format_threshold_alert,
)
