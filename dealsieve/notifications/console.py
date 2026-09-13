"""Console notifier: prints the alert in a bordered ASCII box to stdout.

This is what the local/offline demo shows in place of a real Telegram message.
"""

from __future__ import annotations

import textwrap

from dealsieve.schemas import Channel, Notification


def _render_lines(notification: Notification) -> list[str]:
    lines: list[str] = [notification.title, ""]
    lines.extend(notification.body.splitlines())
    if notification.actions:
        lines.append("")
        lines.append(" ".join(f"[{action.label}]" for action in notification.actions))
    return lines


BOX_WRAP_WIDTH = 96


def _wrap(lines: list[str], width: int = BOX_WRAP_WIDTH) -> list[str]:
    """Word-wrap long lines so the box fits a normal terminal; short lines pass through unchanged."""
    out: list[str] = []
    for line in lines:
        if len(line) <= width:
            out.append(line)
            continue
        indent = "  " if line.startswith("- ") else ""
        out.extend(textwrap.wrap(line, width=width, subsequent_indent=indent, break_long_words=False) or [""])
    return out


def render_box(notification: Notification) -> str:
    """Return the bordered box as a single string (also used by tests)."""
    lines = _wrap(_render_lines(notification))
    width = max((len(line) for line in lines), default=0)
    border = "+" + "-" * (width + 2) + "+"
    body_lines = [f"| {line.ljust(width)} |" for line in lines]
    return "\n".join([border, *body_lines, border])


class ConsoleNotifier:
    name = "console"
    channel = Channel.MANUAL

    def send(self, notification: Notification) -> str | None:
        print(render_box(notification))
        return None
