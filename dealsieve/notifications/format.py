"""Deterministic alert formatting. W4 implements. Plain text, Telegram-safe (no markdown tables).

format_threshold_alert() renders the fixed-column body described in docs/CONTRACTS.md and returns a
Notification. The action row ("[Review] [Draft broker questions] [Ignore]") is NOT baked into ``body`` —
it is carried structurally as ``actions`` so Telegram can render it as an inline keyboard and the console
notifier can render it as a text row inside its box.
"""

from __future__ import annotations

import re
from decimal import Decimal

from dealsieve.schemas import (
    Channel,
    DiligenceRequest,
    DocumentAnalysis,
    GateResult,
    Notification,
    NotificationAction,
    Opportunity,
    OutboundDraft,
    SkepticReport,
    UnderwritingResult,
)

_LABEL_WIDTH = 17
_VALUE_WIDTH = 17
_MAX_UNRESOLVED = 4

_ACTIONS: list[NotificationAction] = [
    NotificationAction(label="Review", action="review"),
    NotificationAction(label="Draft broker questions", action="draft_questions"),
    NotificationAction(label="Ignore", action="ignore"),
]

_PENDING_REQUEST_ACTIONS: list[NotificationAction] = [
    NotificationAction(label="Review", action="review"),
    NotificationAction(label="Approve broker questions", action="approve"),
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
    pending_request: OutboundDraft | None = None,
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
        unresolved = [
            concern.topic for concern in skeptic.concerns if concern.evidence_status in ("missing", "weak")
        ]
        shown, overflow = unresolved[:_MAX_UNRESOLVED], unresolved[_MAX_UNRESOLVED:]
        lines.extend(f"- {topic}" for topic in shown)
        if overflow:
            lines.append(f"- +{len(overflow)} more in the dashboard")

    if pending_request is not None and pending_request.kind == "information_request" and pending_request.status == "pending":
        lines.append("")
        lines.append(
            "Awaiting your approval: information request to the broker "
            f"({len(pending_request.questions)} questions)"
        )

    body = "\n".join(lines)

    return Notification(
        opportunity_id=opportunity.opportunity_id,
        kind="threshold_crossed",
        channel=channel,
        title=title,
        body=body,
        actions=list(_PENDING_REQUEST_ACTIONS if pending_request is not None else _ACTIONS),
    )


def _finding_line(analysis: DocumentAnalysis) -> str | None:
    if not analysis.findings:
        return analysis.summary or None
    rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
    findings = sorted(analysis.findings, key=lambda finding: rank[finding.severity])[:3]
    return "; ".join(
        f"{finding.topic}: {finding.value}" + (f", {finding.detail}" if finding.detail else "")
        for finding in findings
    )


def format_fell_below_alert(
    opportunity: Opportunity,
    previous_run: UnderwritingResult | None,
    new_run: UnderwritingResult,
    analysis: DocumentAnalysis | None,
    credit_draft: OutboundDraft | None,
    *,
    channel: Channel = Channel.TELEGRAM,
) -> Notification:
    """Build the alert emitted when diligence moves a REVIEW deal back below threshold."""
    lines: list[str] = [opportunity.display_name]
    if analysis is not None:
        finding = _finding_line(analysis)
        if finding:
            lines.extend(["", f"Diligence established: {finding}"])
        immediate = sum(
            (item.midpoint for item in analysis.capex_items if item.urgency in {"immediate", "near_term"}),
            Decimal("0"),
        )
        if immediate:
            lines.append(f"Immediate capex added: {_money(immediate)}")

    old_basis = (
        previous_run.financing.all_in_basis or previous_run.financing.purchase_price
        if previous_run
        else None
    )
    new_basis = new_run.financing.all_in_basis or new_run.financing.purchase_price
    cap_gate = _find_gate(new_run, "min_normalized_cap_rate")
    dscr_gate = _find_gate(new_run, "min_base_dscr")
    ltv_gate = _find_gate(new_run, "max_ltv")
    lines.append("")
    lines.append(
        _row(
            "Price basis",
            f"{_money(old_basis)} -> {_money(new_basis)}" if old_basis is not None else _money(new_basis),
        )
    )
    lines.append(
        _row(
            "Normalized cap",
            (
                f"{_pct(previous_run.normalized.normalized_cap_rate)} -> "
                f"{_pct(new_run.normalized.normalized_cap_rate)}"
                if previous_run
                else _pct(new_run.normalized.normalized_cap_rate)
            ),
            _status(cap_gate),
        )
    )
    lines.append(
        _row(
            "DSCR",
            (
                f"{_dscr(previous_run.financing.dscr)} -> {_dscr(new_run.financing.dscr)}"
                if previous_run
                else _dscr(new_run.financing.dscr)
            ),
            _status(dscr_gate),
        )
    )
    lines.append(
        _row(
            "LTV",
            (
                f"{_pct(previous_run.financing.ltv)} -> {_pct(new_run.financing.ltv)}"
                if previous_run
                else _pct(new_run.financing.ltv)
            ),
            _status(ltv_gate),
        )
    )
    lines.extend(["", f"New status: {new_run.status.value}"])
    frontier = new_run.viability.max_viable_price
    if frontier is not None:
        distance = new_run.viability.distance_pct
        suffix = f", {_pct(distance, decimals=1)} under the ask" if distance is not None else ""
        lines.append(f"Viable below {_money(frontier)}{suffix}")
    if credit_draft is not None:
        amount_match = re.search(r"\$[\d,]+", credit_draft.body)
        amount = amount_match.group(0) if amount_match else "the required"
        lines.extend(["", f"Drafted for your approval: request a {amount} credit."])

    return Notification(
        opportunity_id=opportunity.opportunity_id,
        kind="fell_below_threshold",
        channel=channel,
        title=f"DEAL #{opportunity.deal_number} FELL BACK BELOW THRESHOLD",
        body="\n".join(lines),
        actions=[
            NotificationAction(label="Review", action="review"),
            NotificationAction(label="Approve", action="approve"),
            NotificationAction(label="Reject", action="reject"),
        ],
    )


def format_stalled_alert(
    opportunity: Opportunity,
    requests: list[DiligenceRequest],
    *,
    channel: Channel = Channel.TELEGRAM,
) -> Notification:
    """Build the one-time escalation after the autonomous follow-up budget is exhausted."""
    follow_ups = max((request.follow_up_count for request in requests), default=0)
    topics = ", ".join(request.topic for request in requests)
    body = (
        f"{opportunity.display_name}\n\n"
        f"No reply on {len(requests)} requests after {follow_ups} follow-ups: {topics}. "
        "The loop has stopped; your move."
    )
    return Notification(
        opportunity_id=opportunity.opportunity_id,
        kind="diligence_stalled",
        channel=channel,
        title=f"DEAL #{opportunity.deal_number} DILIGENCE STALLED",
        body=body,
        actions=[
            NotificationAction(label="Review", action="review"),
            NotificationAction(label="Ignore", action="ignore"),
        ],
    )
