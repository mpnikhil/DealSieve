"""Parse RFC 822 .eml (bytes or path) into InboundMessage.

W2 implements: message_id from Message-ID header (fallback sha256 of raw), sender/sender_name from From,
subject, body_text (prefer text/plain, else html->text), thread_id from References/In-Reply-To chain root,
attachments with sha256 and extracted text for application/pdf (pypdf), text/*, and .md/.txt/.csv.
Also collect URLs found in the body into InboundMessage.urls. Preserve raw bytes to data/raw/<message_id>.eml
when raw_dir is given and set raw_ref.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import html as html_module
import os
import re
from datetime import UTC
from io import BytesIO
from pathlib import Path

import pypdf

from dealsieve.schemas import Attachment, Channel, InboundMessage

_ID_RE = re.compile(r"<([^<>]+)>")
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_WHITESPACE_RE = re.compile(r"\s+")
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_.-]")

_TEXT_EXTENSIONS = {".md", ".txt", ".csv"}


def _strip_angle(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    match = _ID_RE.search(value)
    return match.group(1) if match else (value or None)


def _first_ref_id(value: str | None) -> str | None:
    if not value:
        return None
    ids = _ID_RE.findall(value)
    return ids[0] if ids else None


def _html_to_text(html_content: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(" ", html_content)
    text = _TAG_RE.sub(" ", text)
    text = html_module.unescape(text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _extract_urls(text: str) -> list[str]:
    return _URL_RE.findall(text or "")


def _sanitize_filename(name: str) -> str:
    return _SANITIZE_RE.sub("_", name)


def _attachment_text(content_type: str, filename: str | None, raw_bytes: bytes) -> str | None:
    ext = Path(filename).suffix.lower() if filename else ""
    if content_type.lower().startswith("text/") or ext in _TEXT_EXTENSIONS:
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return raw_bytes.decode("utf-8", errors="replace")
    if content_type.lower() == "application/pdf" or ext == ".pdf":
        try:
            reader = pypdf.PdfReader(BytesIO(raw_bytes))
            pages = [page.extract_text() or "" for page in reader.pages]
            joined = "\n".join(pages).strip()
            return joined or None
        except Exception:
            return None
    return None


def _load_raw(source: bytes | str | os.PathLike[str]) -> bytes:
    if isinstance(source, bytes):
        return source
    if isinstance(source, (str, os.PathLike)):
        return Path(source).read_bytes()
    raise TypeError(f"parse_eml: unsupported source type {type(source)!r}")


def parse_eml(
    source: bytes | str | os.PathLike[str], *, raw_dir: str | os.PathLike[str] | None = None
) -> InboundMessage:
    raw = _load_raw(source)
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    message_id = _strip_angle(msg.get("Message-ID")) or hashlib.sha256(raw).hexdigest()

    name, addr = email.utils.parseaddr(msg.get("From", ""))
    sender = addr or None
    sender_name = name or None

    subject = msg.get("Subject")
    subject = str(subject) if subject is not None else None

    body_text = ""
    body_part = msg.get_body(preferencelist=("plain", "html"))
    if body_part is not None:
        content = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            body_text = _html_to_text(content)
        else:
            body_text = content
    urls = _extract_urls(body_text)

    attachments: list[Attachment] = []
    for idx, part in enumerate(msg.iter_attachments()):
        raw_bytes = part.get_payload(decode=True) or b""
        filename = part.get_filename() or f"attachment_{idx}"
        content_type = part.get_content_type()
        attachments.append(
            Attachment(
                filename=filename,
                content_type=content_type,
                sha256=hashlib.sha256(raw_bytes).hexdigest(),
                size_bytes=len(raw_bytes),
                text=_attachment_text(content_type, filename, raw_bytes),
            )
        )

    references_root = _first_ref_id(msg.get("References"))
    in_reply_to = _strip_angle(msg.get("In-Reply-To"))
    thread_id = references_root or in_reply_to or message_id

    received_at = None
    date_header = msg.get("Date")
    if date_header:
        try:
            parsed_date = email.utils.parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            parsed_date = None
        if parsed_date is not None:
            received_at = parsed_date if parsed_date.tzinfo else parsed_date.replace(tzinfo=UTC)

    kwargs: dict = dict(
        message_id=message_id,
        channel=Channel.EMAIL,
        sender=sender,
        sender_name=sender_name,
        subject=subject,
        body_text=body_text,
        attachments=attachments,
        urls=urls,
        in_reply_to=in_reply_to,
        thread_id=thread_id,
    )
    if received_at is not None:
        kwargs["received_at"] = received_at

    message = InboundMessage(**kwargs)

    if raw_dir is not None:
        dir_path = Path(raw_dir)
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / f"{_sanitize_filename(message_id)}.eml"
        file_path.write_bytes(raw)
        message = message.model_copy(update={"raw_ref": str(file_path)})

    return message
