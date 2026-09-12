"""Parse RFC 822 .eml (bytes or path) into InboundMessage.

W2 implements: message_id from Message-ID header (fallback sha256 of raw), sender/sender_name from From,
subject, body_text (prefer text/plain, else html->text), thread_id from References/In-Reply-To chain root,
attachments with sha256 and extracted text for application/pdf (pypdf), text/*, and .md/.txt/.csv.
Also collect URLs found in the body into InboundMessage.urls. Preserve raw bytes to data/raw/<message_id>.eml
when raw_dir is given and set raw_ref.
"""

from __future__ import annotations

import os

from dealsieve.schemas import InboundMessage


def parse_eml(source: bytes | str | os.PathLike[str], *, raw_dir: str | os.PathLike[str] | None = None) -> InboundMessage:
    raise NotImplementedError("W2: dealsieve.ingestion.email.parse_eml")
