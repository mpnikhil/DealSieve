"""The inspector agent: reads one diligence document, text and photographs together.

This is the multimodal step of the diligence loop. A broker replies with a 40-page property
condition report; the inspector reads the text, looks at the embedded photographs, and produces one
immutable :class:`~dealsieve.schemas.DocumentAnalysis`: what the document established, which open
questions it answers, and what capital work it implies.

Two rules shape the prompt:

1. **Provenance or nothing.** Every finding cites a page or an image, so the dashboard can show the
   reader exactly where a $90,000 capex number came from. Findings without provenance are worth
   very little in a diligence record.
2. **Never invent a number.** The model is told, repeatedly, that a figure it cannot point at in the
   text or see in a photograph must not appear. Capital numbers here move the underwriting basis.

Images are addressed as "image N" in the prompt (a text block "image N" immediately precedes each
image content block) and the model cites them the same way. :func:`run_inspector` then rewrites each
``image_ref`` from "image N" to the actual stored path, so both the scripted and the live backends
produce identical records. Providers that cannot accept images (see ``models/cli_model.py``) are told
so explicitly and leave ``image_ref`` null rather than describing a photo they never saw.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field
from strands import Agent

from dealsieve.models import get_model
from dealsieve.models.cli_model import image_format_for
from dealsieve.schemas import (
    Attachment,
    CapexItem,
    DiligenceRequest,
    DocumentAnalysis,
    DocumentFinding,
    ModelPurpose,
    RequestAnswer,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from dealsieve.agents.tools import ProcessingSession

logger = logging.getLogger(__name__)

DOCUMENT_TEXT_CAP = 40_000
IMAGE_REF_PATTERN = re.compile(r"^\s*image\s*(\d+)\s*$", re.IGNORECASE)

DocumentType = Literal[
    "inspection_report",
    "roof_report",
    "phase_i",
    "cam_statement",
    "rent_roll",
    "lease",
    "offering_memorandum",
    "other",
]

SYSTEM_PROMPT = """\
You are a property-condition analyst for a private buyer of small-bay multi-tenant industrial \
property. A document has arrived about a property the buyer is underwriting. Read it -- the text and \
every photograph -- and report what it establishes.

Rules:
- NEVER state a number that is not written in the document text or plainly visible in a photograph. \
No estimating, no "typical" costs, no rounding a range into a point. If the document gives a range, \
keep the range.
- Cite provenance on every finding: `page` when the text says which page it is on, and `image_ref` \
as exactly "image 1", "image 2", ... for anything you concluded by looking at a photograph. A \
finding with neither is much less useful; prefer to cite something.
- For each photograph, say what is actually visible in it. Do not describe an image you were not \
shown -- if the prompt says images were not available to you, leave every `image_ref` null and work \
from the text alone.
- `capex_items`: one per distinct capital item the document prices, with the document's own low and \
high figures. `urgency` is "immediate" when the document says now or within about a year, \
"near_term" for roughly one to three years, "deferred" beyond that. A recommendation to replace \
within 12 to 24 months is "immediate".
- `answers`: only for the open requests listed in the prompt, matched by their topic. Set \
`resolves` true only when the document actually settles that question; a passing mention is not an \
answer.
- `red_flags`: conditions that change the risk of owning the building, not routine maintenance.
- `severity`: "high" when it costs six figures or threatens the income, "medium" when it changes \
the price, "low" for a formality, "info" for context.
- Be concrete and short. No hedging prose, no restating the whole document.
"""


# --------------------------------------------------------------------------- structured output


class DocumentFindingOutput(BaseModel):
    """One thing the document establishes, with where it was established."""

    topic: str = Field(description='Short noun phrase, e.g. "Roof age", "Ponding water", "HVAC vintage".')
    value: str = Field(description="What the document says about it, in one line, with its own numbers.")
    detail: str | None = Field(default=None, description="One or two sentences of supporting specifics.")
    severity: Literal["info", "low", "medium", "high"] = "info"
    confidence: float = Field(ge=0.0, le=1.0, description="1.0 when the document states it outright.")
    page: int | None = Field(default=None, description="Page number when the document identifies one.")
    image_ref: str | None = Field(
        default=None, description='Exactly "image 1", "image 2", ... when this rests on a photograph.'
    )


class CapexItemOutput(BaseModel):
    """A capital item the document prices. Only items the document itself costs out."""

    item: str
    low: float = Field(description="The document's low figure, in dollars.")
    high: float = Field(description="The document's high figure, in dollars.")
    urgency: Literal["immediate", "near_term", "deferred"]
    location: str | None = Field(default=None, description='e.g. "page 3", "findings table".')


class RequestAnswerOutput(BaseModel):
    """An answer to one of the open diligence requests listed in the prompt."""

    request_topic: str = Field(description="The topic of the open request, copied exactly.")
    answer: str = Field(description="What the document says, in one or two lines.")
    resolves: bool = Field(description="True only when the document fully settles the question.")


class DocumentAnalysisOutput(BaseModel):
    """The inspector's structured reading of one document (the id-free mirror of DocumentAnalysis)."""

    document_type: DocumentType
    summary: str = Field(description="Two or three lines: what this document is and what it establishes.")
    findings: list[DocumentFindingOutput] = Field(default_factory=list)
    answers: list[RequestAnswerOutput] = Field(default_factory=list)
    capex_items: list[CapexItemOutput] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    images_reviewed: int = Field(default=0, description="How many of the supplied photographs you looked at.")


# --------------------------------------------------------------------------- prompt


def _render_open_requests(open_requests: list[DiligenceRequest]) -> str:
    if not open_requests:
        return "(none -- report what the document establishes, with no answers section)"
    return "\n".join(f"- {r.topic}: {r.question}" for r in open_requests)


def load_images(attachment: Attachment) -> list[tuple[str, str, bytes]]:
    """Read `attachment.image_paths` into (path, strands image format, bytes), skipping unreadable ones."""
    loaded: list[tuple[str, str, bytes]] = []
    for raw in attachment.image_paths:
        path = Path(raw)
        try:
            data = path.read_bytes()
        except OSError as exc:
            logger.warning("document image %s could not be read: %s", raw, exc)
            continue
        if not data:
            continue
        loaded.append((str(raw), image_format_for(path), data))
    return loaded


def build_inspector_prompt(
    attachment: Attachment,
    open_requests: list[DiligenceRequest],
    *,
    image_count: int,
    text_cap: int = DOCUMENT_TEXT_CAP,
) -> str:
    """The text half of the user message: open questions, then the document itself."""
    text = attachment.text or ""
    truncated = len(text) > text_cap
    body = text[:text_cap] + ("\n[... truncated ...]" if truncated else "")

    if image_count:
        images_line = (
            f"{image_count} photograph(s) from this document follow the text below, each preceded by a "
            'line naming it ("image 1", "image 2", ...). Cite them by exactly that name in `image_ref`.'
        )
    else:
        images_line = "This document carried no extractable photographs. Leave every `image_ref` null."

    return "\n".join(
        [
            f"# Document: {attachment.filename} ({attachment.content_type})",
            "",
            "## Open diligence requests (the questions we are waiting on)",
            _render_open_requests(open_requests),
            "",
            "## Images",
            images_line,
            "",
            "## Document text",
            body or "(no extractable text)",
            "",
            "Report your structured reading now. Every number must come from the text above or be "
            "visible in one of the photographs.",
        ]
    )


def build_content_blocks(
    attachment: Attachment,
    open_requests: list[DiligenceRequest],
    images: list[tuple[str, str, bytes]],
    *,
    text_cap: int = DOCUMENT_TEXT_CAP,
) -> list[dict[str, Any]]:
    """The Strands multimodal user message: one text block, then "image N" + image, per photo."""
    blocks: list[dict[str, Any]] = [
        {
            "text": build_inspector_prompt(
                attachment, open_requests, image_count=len(images), text_cap=text_cap
            )
        }
    ]
    for index, (_path, fmt, data) in enumerate(images, start=1):
        blocks.append({"text": f"image {index}"})
        blocks.append({"image": {"format": fmt, "source": {"bytes": data}}})
    return blocks


# --------------------------------------------------------------------------- mapping


def resolve_image_ref(raw: str | None, image_paths: list[str]) -> str | None:
    """Turn the model's "image N" citation into the stored path it refers to.

    Out-of-range or unparsable references are dropped rather than guessed: a citation that points at
    nothing is worse than no citation, because the dashboard would link to the wrong photograph.
    """
    if not raw:
        return None
    match = IMAGE_REF_PATTERN.match(raw)
    if not match:
        return raw if raw in image_paths else None
    index = int(match.group(1))
    if 1 <= index <= len(image_paths):
        return image_paths[index - 1]
    logger.warning("finding cites %r but only %d image(s) were supplied", raw, len(image_paths))
    return None


def to_document_analysis(
    output: DocumentAnalysisOutput,
    *,
    opportunity_id: str,
    message_id: str,
    attachment: Attachment,
    image_paths: list[str],
    model_backend: str | None,
) -> DocumentAnalysis:
    """Build the immutable record from the model's id-free output."""
    findings = [
        DocumentFinding(
            topic=f.topic,
            value=f.value,
            detail=f.detail,
            severity=f.severity,
            confidence=f.confidence,
            page=f.page,
            image_ref=resolve_image_ref(f.image_ref, image_paths),
        )
        for f in output.findings
    ]
    capex_items = [
        CapexItem(
            item=c.item,
            low=str(c.low),
            high=str(c.high),
            urgency=c.urgency,
            source_document=attachment.filename,
            location=c.location,
        )
        for c in output.capex_items
    ]
    return DocumentAnalysis(
        opportunity_id=opportunity_id,
        message_id=message_id,
        filename=attachment.filename,
        document_type=output.document_type,
        summary=output.summary,
        findings=findings,
        answers=[
            RequestAnswer(request_topic=a.request_topic, answer=a.answer, resolves=a.resolves)
            for a in output.answers
        ],
        capex_items=capex_items,
        red_flags=list(output.red_flags),
        images_reviewed=len(image_paths),
        image_paths=list(image_paths),
        text_chars=len(attachment.text or ""),
        model_backend=model_backend,
    )


# --------------------------------------------------------------------------- entry point


def run_inspector(
    session: ProcessingSession,
    attachment: Attachment,
    open_requests: list[DiligenceRequest],
) -> DocumentAnalysis:
    """Read one document with a multimodal Strands agent and return a storable analysis."""
    if session.opportunity_id is None:
        raise ValueError("the inspector needs an opportunity; record_claims must run first")

    images = load_images(attachment)
    image_paths = [path for path, _fmt, _data in images]

    agent = Agent(
        model=get_model(ModelPurpose.DOCUMENT, script=session.script),
        tools=[],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
    )
    result = agent(
        build_content_blocks(attachment, open_requests, images),
        structured_output_model=DocumentAnalysisOutput,
    )
    output = result.structured_output
    if not isinstance(output, DocumentAnalysisOutput):  # pragma: no cover - defensive
        raise ValueError("the inspector agent did not return a DocumentAnalysisOutput")

    return to_document_analysis(
        output,
        opportunity_id=session.opportunity_id,
        message_id=session.message.message_id,
        attachment=attachment,
        image_paths=image_paths,
        model_backend=session.model_backend,
    )


__all__ = [
    "DOCUMENT_TEXT_CAP",
    "SYSTEM_PROMPT",
    "CapexItemOutput",
    "DocumentAnalysisOutput",
    "DocumentFindingOutput",
    "RequestAnswerOutput",
    "build_content_blocks",
    "build_inspector_prompt",
    "load_images",
    "resolve_image_ref",
    "run_inspector",
    "to_document_analysis",
]
