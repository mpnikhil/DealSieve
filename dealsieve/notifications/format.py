"""Deterministic alert formatting. W4 implements. Plain text, Telegram-safe (no markdown tables).

format_threshold_alert() renders the fixed-column body described in docs/CONTRACTS.md and returns a
Notification. The action row ("[Review] [Draft broker questions] [Ignore]") is NOT baked into ``body`` —
it is carried structurally as ``actions`` so Telegram can render it as an inline keyboard and the console
notifier can render it as a text row inside its box.
"""

from __future__ import annotations

from decimal import Decimal

from dealsieve.schemas import (
    Channel,
    GateResult,
    Notification,
    NotificationAction,
    Opportunity,
    SkepticReport,
    UnderwritingResult,
)

_LABEL_WIDTH = 17
_VALUE_WIDTH = 17

_ACTIONS: list[NotificationAction] = [
    NotificationAction(label="Review", action="review"),
    NotificationAction(label="Draft broker questions", action="draft_questions"),
    NotificationAction(label="Ignore", action="ignore"),
]


def _money(value: Decimal) -> str:
    return f"${value:,.0f}"


def _pct(value: Decimal, *, decimals: int = 2) -> str:
    return f"{value * 100:.{decimals}f}%"


def _dscr(value: Decimal) -> str:
    return f"{value:.2f}x"


def _find_gate(run: UnderwritingResult, key: str) -> GateResult | None:
    for gate in run.gates:
        if gate.gate == key:
            return gate
    return None


def _status(gate: GateResult | None) -> str:
    if gate is None:
        return ""
    return "PASS" if gate.passed else "FAIL"


def _row(label: str, value: str, status: str = "") -> str:
    if status:
        return f"{label:<{_LABEL_WIDTH}}{value:<{_VALUE_WIDTH}}{status}"
    return f"{label:<{_LABEL_WIDTH}}{value}"


def format_threshold_alert(
    opportunity: Opportunity,
    previous_run: UnderwritingResult | None,
    new_run: UnderwritingResult,
    skeptic: SkepticReport | None,
    *,
    channel: Channel = Channel.TELEGRAM,
) -> Notification:
    """Build the "DEAL #<n> JUST BECAME INVESTABLE" alert.

    ``previous_run`` may be None (e.g. the very first run already lands in REVIEW); in that case the
    metric rows show only the current value with no "before -> after" arrow, and the "Previously failed
    solely on valuation." line is omitted (there is no previous run to characterize).
    """
    title = f"DEAL #{opportunity.deal_number} JUST BECAME INVESTABLE"

    cap_gate = _find_gate(new_run, "min_normalized_cap_rate")
    dscr_gate = _find_gate(new_run, "min_base_dscr")
    tenant_gate = _find_gate(new_run, "largest_tenant_pct_max")

    if previous_run is not None:
        price_value = f"{_money(previous_run.inputs.asking_price)} -> {_money(new_run.inputs.asking_price)}"
        cap_value = (
            f"{_pct(previous_run.normalized.normalized_cap_rate)} -> "
            f"{_pct(new_run.normalized.normalized_cap_rate)}"
        )
        dscr_value = f"{_dscr(previous_run.financing.dscr)} -> {_dscr(new_run.financing.dscr)}"
    else:
        price_value = _money(new_run.inputs.asking_price)
        cap_value = _pct(new_run.normalized.normalized_cap_rate)
        dscr_value = _dscr(new_run.financing.dscr)

    largest_tenant_pct = new_run.inputs.largest_tenant_pct
    tenant_value = _pct(largest_tenant_pct, decimals=0) if largest_tenant_pct is not None else "unknown"

    lines = [
        opportunity.display_name,
        "",
        _row("Price", price_value),
        _row("Normalized cap", cap_value, _status(cap_gate)),
        _row("DSCR", dscr_value, _status(dscr_gate)),
        _row("Largest tenant", tenant_value, _status(tenant_gate)),
    ]

    if previous_run is not None and not previous_run.viability.structural_failures:
        lines.append("")
        lines.append("Previously failed solely on valuation.")

    if skeptic is not None:
        lines.append("")
        lines.append("Still unresolved:")
        lines.extend(
            f"- {concern.topic}" for concern in skeptic.concerns if concern.evidence_status in ("missing", "weak")
        )

    body = "\n".join(lines)

    return Notification(
        opportunity_id=opportunity.opportunity_id,
        kind="threshold_crossed",
        channel=channel,
        title=title,
        body=body,
        actions=list(_ACTIONS),
    )
