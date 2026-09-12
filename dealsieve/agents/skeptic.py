"""The skeptic agent: an independent second opinion whose job is to find the reason to say no.

It runs only when the deterministic engine has already said REVIEW. It has no tools, cannot touch
the database, and is explicitly forbidden from recomputing finance. It returns structured output
so its concerns can be stored, shown and turned into broker questions.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field
from strands import Agent

from dealsieve.agents.tools import ProcessingSession
from dealsieve.models import get_model
from dealsieve.policy import InvestmentPolicy
from dealsieve.schemas import (
    Evidence,
    ModelPurpose,
    SkepticConcern,
    SkepticReport,
    UnderwritingResult,
)

OM_EXCERPT_CAP = 12_000

SYSTEM_PROMPT = """\
You are an independent skeptic reviewing a commercial real estate acquisition that has just passed \
every hard underwriting gate. Your employer has been burned before by deals that looked clean on a \
spreadsheet. Your job is to find the reasons this deal should still be rejected, or the questions \
that must be answered before anyone spends another hour on it.

Rules:
- NEVER recompute or second-guess the arithmetic. The cap rate, DSCR, expense normalization and \
viability frontier were produced by a deterministic engine under a frozen policy. Treat them as \
given. Your concern is what the numbers are built on, not the numbers themselves.
- Attack the evidence. For every material input, ask: who asserted this, in what document, and was \
it ever verified? A number stated only by the seller's broker is not verified.
- The usual killers in small-bay industrial, all of which are typically *absent* from an offering \
memorandum and should be raised whenever they are not evidenced:
  * roof age and replacement history (a $200k roof erases years of cash flow),
  * a Phase I environmental report (industrial tenants: solvents, oil, machining, plating),
  * CAM reconciliation for the trailing year (recoveries the broker assumes may not be collectible),
  * lease rollover concentration and below-market or short-remaining-term leases,
  * tenants related to the seller, or leases signed just before listing,
  * deferred maintenance, unpermitted improvements, and actual vs. pro-forma occupancy.
- Use exactly these evidence_status values: "missing" when nothing in the record addresses it, \
"weak" when it is asserted without support, "contradicted" when sources disagree, "unverified" \
when it is plausible but unchecked.
- Severity: "high" if it could kill the deal or cost six figures, "medium" if it changes the price, \
"low" if it is a diligence formality.
- Give each concern a `question_for_broker`: one specific, answerable question.
- Verdict: "reject" if something in the record already disqualifies it, "proceed_with_questions" \
if it survives only once the gaps are answered (the usual answer), "proceed" if the record is \
genuinely complete.
- Be concrete and short. No hedging prose, no restating the financials.
"""


class SkepticConcernOutput(BaseModel):
    """One reason to hesitate, with what would resolve it."""

    topic: str = Field(description='Short label, e.g. "roof age", "Phase I environmental".')
    severity: Literal["low", "medium", "high"]
    why_it_matters: str = Field(description="One or two sentences on the concrete downside.")
    evidence_status: Literal["missing", "weak", "contradicted", "unverified"]
    question_for_broker: str | None = Field(
        default=None, description="One specific question that would resolve this concern."
    )


class SkepticOutput(BaseModel):
    """The skeptic's structured verdict on an opportunity that passed every gate."""

    verdict: Literal["proceed", "proceed_with_questions", "reject"]
    summary: str = Field(description="One or two lines: what would have to be true for this to work.")
    concerns: list[SkepticConcernOutput] = Field(default_factory=list)


# --------------------------------------------------------------------------- prompt


def _pct(value: Decimal | None) -> str:
    return "—" if value is None else f"{Decimal(value) * 100:.2f}%"


def _money(value: Decimal | None) -> str:
    return "—" if value is None else f"${Decimal(value):,.0f}"


def _policy_summary(policy: InvestmentPolicy) -> str:
    return "\n".join(
        [
            f"policy_version: {policy.policy_version} ({policy.name})",
            f"minimum normalized cap rate: {_pct(policy.underwriting.min_normalized_cap_rate)}",
            f"minimum base DSCR: {policy.underwriting.min_base_dscr}x",
            f"maximum LTV: {_pct(policy.financing.max_ltv)}",
            f"absolute maximum price: {_money(policy.purchase.absolute_max)}",
            f"minimum tenant count: {policy.property.tenant_count_min}",
            f"maximum largest-tenant share of rent: {_pct(policy.property.largest_tenant_pct_max)}",
            f"assumed debt: {_pct(policy.financing.assumed_interest_rate)} over "
            f"{policy.financing.amortization_years} years",
        ]
    )


def _run_summary(run: UnderwritingResult) -> str:
    gates = "\n".join(
        f"  - {g.gate} ({g.kind.value}): {'PASS' if g.passed else 'FAIL'} "
        f"[{g.comparator} {g.threshold}, actual {g.actual}] {g.description}"
        for g in run.gates
    )
    comparison = "\n".join(
        f"  - {row.metric}: broker {row.broker or '—'} vs DealSieve {row.dealsieve}"
        + (f" ({row.note})" if row.note else "")
        for row in run.comparison
    )
    stress = "\n".join(
        f"  - {s.scenario}: NOI {_money(s.noi)}, DSCR {Decimal(s.dscr):.2f}x, "
        f"{'covers debt' if s.covers_debt else 'DOES NOT cover debt'}"
        for s in run.stress
    )
    expenses = "\n".join(
        f"  - {line.name}: broker {_money(line.broker)}, normalized {_money(line.normalized)} ({line.basis})"
        for line in run.normalized.expenses
    )
    return "\n".join(
        [
            f"status: {run.status.value}",
            f"asking price: {_money(run.inputs.asking_price)}",
            f"broker NOI {_money(run.normalized.broker_noi)} at cap {_pct(run.normalized.broker_cap_rate)}; "
            f"normalized NOI {_money(run.normalized.noi)} at cap {_pct(run.normalized.normalized_cap_rate)}",
            f"DSCR {Decimal(run.financing.dscr):.2f}x, LTV {_pct(run.financing.ltv)}, "
            f"loan {_money(run.financing.loan_amount)}, equity {_money(run.financing.equity_deployed)}",
            "expenses:",
            expenses,
            "gates:",
            gates,
            "broker vs DealSieve:",
            comparison,
            "stress:",
            stress,
            f"summary: {run.failure_summary}",
        ]
    )


def _evidence_summary(evidence: list[Evidence]) -> str:
    if not evidence:
        return "  (no evidence recorded)"
    lines = []
    for item in evidence:
        quote = f' "{item.quote}"' if item.quote else ""
        lines.append(
            f"  - {item.field} = {item.value} | source: {item.source_document}"
            f" ({item.location or 'unspecified location'}) | confidence {item.confidence:.2f}{quote}"
        )
    return "\n".join(lines)


def build_skeptic_prompt(session: ProcessingSession) -> str:
    """Render everything the skeptic is allowed to see."""
    run = session.run_after
    if run is None:  # pragma: no cover - guarded by the caller
        raise ValueError("the skeptic needs a completed underwriting run")
    opp = session.opportunity()
    repo = session.repo
    opportunity_id = session.opportunity_id or run.opportunity_id

    evidence = repo.list_evidence(opportunity_id)
    working = run.inputs
    claims = session.claims

    om_sections: list[str] = []
    for attachment in session.message.attachments:
        if attachment.text:
            text = attachment.text[:OM_EXCERPT_CAP]
            suffix = "\n[... truncated ...]" if len(attachment.text) > OM_EXCERPT_CAP else ""
            om_sections.append(f"### {attachment.filename}\n{text}{suffix}")

    parts = [
        f"# Deal #{opp.deal_number if opp else '?'}: {opp.display_name if opp else opportunity_id}",
        "",
        "## Investment policy (frozen, not up for debate)",
        _policy_summary(session.policy),
        "",
        "## Latest deterministic underwriting run (do not recompute)",
        _run_summary(run),
        "",
        "## Evidence on file (field = value, with provenance)",
        _evidence_summary(evidence),
        "",
        "## Fields the extractor could not find",
        ", ".join(claims.missing_fields) if claims and claims.missing_fields else "(none reported)",
        "",
        "## Unresolved conflicts between sources",
        "\n".join(f"  - {c}" for c in working.conflicts) if working.conflicts else "(none)",
        "",
        "## Rent roll as recorded",
        "\n".join(
            f"  - {t.name} | suite {t.suite or '—'} | {t.sqft or '—'} sf | "
            f"{_money(t.annual_rent)} | lease end {t.lease_end or 'unknown'}"
            for t in working.tenants
        )
        or "  (no rent roll recorded)",
        "",
        "## Source documents",
        "\n\n".join(om_sections) if om_sections else "(no attachment text on this message)",
        "",
        "Return your structured verdict now. Find what is missing.",
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- entry point


def run_skeptic(session: ProcessingSession) -> SkepticReport:
    """Run the independent skeptic agent and return a storable report."""
    run = session.run_after
    if run is None:
        raise ValueError("the skeptic needs a completed underwriting run")

    agent = Agent(
        model=get_model(ModelPurpose.SKEPTIC, script=session.script),
        tools=[],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
    )
    result = agent(build_skeptic_prompt(session), structured_output_model=SkepticOutput)
    output = result.structured_output
    if not isinstance(output, SkepticOutput):  # pragma: no cover - defensive
        raise ValueError("the skeptic agent did not return a SkepticOutput")

    return SkepticReport(
        opportunity_id=run.opportunity_id,
        run_id=run.run_id,
        verdict=output.verdict,
        summary=output.summary,
        concerns=[
            SkepticConcern(
                topic=c.topic,
                severity=c.severity,
                why_it_matters=c.why_it_matters,
                evidence_status=c.evidence_status,
                question_for_broker=c.question_for_broker,
            )
            for c in output.concerns
        ],
    )


__all__ = [
    "OM_EXCERPT_CAP",
    "SYSTEM_PROMPT",
    "SkepticConcernOutput",
    "SkepticOutput",
    "build_skeptic_prompt",
    "run_skeptic",
]
