from __future__ import annotations

import base64
from pathlib import Path

from dealsieve.ingestion.email import parse_eml
from dealsieve.ingestion.text import from_text
from dealsieve.schemas import Channel


def _write_eml(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_bytes(content.encode("utf-8"))
    return path


_INITIAL_EML = """From: Jane Broker <jane@brokerage.example>
To: buyer@example.com
Subject: Off-market: 8-unit small-bay industrial, Sacramento
Message-ID: <om-2026-0912-power-inn@brokerage.example>
Date: Sat, 12 Sep 2026 10:00:00 -0700
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUNDARY1"

--BOUNDARY1
Content-Type: text/plain; charset=utf-8

Asking $1,550,000, NOI $126,000. Offering memo attached. Listing: https://example.com/listing/9001

--BOUNDARY1
Content-Type: text/markdown; name="Power_Inn_OM.md"
Content-Disposition: attachment; filename="Power_Inn_OM.md"
Content-Transfer-Encoding: base64

{om_b64}

--BOUNDARY1--
"""


_REPLY_EML = """From: Jane Broker <jane@brokerage.example>
To: buyer@example.com
Subject: Re: Off-market: 8-unit small-bay industrial, Sacramento
Message-ID: <reply-1@brokerage.example>
In-Reply-To: <om-2026-0912-power-inn@brokerage.example>
References: <om-2026-0912-power-inn@brokerage.example>
Date: Sun, 13 Sep 2026 09:00:00 -0700
Content-Type: text/plain; charset=utf-8

Seller reduced this to $1.25M. Any interest?
"""

_HTML_ONLY_EML = """From: broker@example.com
To: buyer@example.com
Subject: HTML only
Message-ID: <html-only@example.com>
Content-Type: text/html; charset=utf-8

<html><body><p>Asking <b>$900,000</b>. See https://example.com/deal/2</p></body></html>
"""

_NO_MESSAGE_ID_EML = b"""From: nobody@example.com
Subject: no id here
Content-Type: text/plain

hello world
"""


def test_parse_eml_extracts_headers_body_and_markdown_attachment(tmp_path):
    om_text = "# Power Inn OM\n\nRent Roll table goes here."
    om_b64 = base64.b64encode(om_text.encode("utf-8")).decode("ascii")
    eml_path = _write_eml(tmp_path, "01_initial_offer.eml", _INITIAL_EML.format(om_b64=om_b64))

    msg = parse_eml(eml_path)

    assert msg.message_id == "om-2026-0912-power-inn@brokerage.example"
    assert msg.channel == Channel.EMAIL
    assert msg.sender == "jane@brokerage.example"
    assert msg.sender_name == "Jane Broker"
    assert msg.subject == "Off-market: 8-unit small-bay industrial, Sacramento"
    assert "Asking $1,550,000" in msg.body_text
    assert "https://example.com/listing/9001" in msg.urls
    assert msg.thread_id == "om-2026-0912-power-inn@brokerage.example"
    assert msg.in_reply_to is None

    assert len(msg.attachments) == 1
    attachment = msg.attachments[0]
    assert attachment.filename == "Power_Inn_OM.md"
    assert attachment.content_type == "text/markdown"
    assert attachment.text == om_text
    assert len(attachment.sha256) == 64
    assert attachment.size_bytes == len(om_text.encode("utf-8"))


def test_parse_eml_reply_uses_in_reply_to_and_references_for_thread(tmp_path):
    eml_path = _write_eml(tmp_path, "02_price_drop.eml", _REPLY_EML)
    msg = parse_eml(eml_path)

    assert msg.message_id == "reply-1@brokerage.example"
    assert msg.in_reply_to == "om-2026-0912-power-inn@brokerage.example"
    assert msg.thread_id == "om-2026-0912-power-inn@brokerage.example"
    assert "Seller reduced this to $1.25M" in msg.body_text
    assert msg.attachments == []


def test_parse_eml_falls_back_to_html_stripped_when_no_plain_text(tmp_path):
    eml_path = _write_eml(tmp_path, "html_only.eml", _HTML_ONLY_EML)
    msg = parse_eml(eml_path)

    assert "Asking" in msg.body_text
    assert "$900,000" in msg.body_text
    assert "<b>" not in msg.body_text
    assert "https://example.com/deal/2" in msg.urls


def test_parse_eml_missing_message_id_falls_back_to_sha256_of_raw():
    msg = parse_eml(_NO_MESSAGE_ID_EML)
    assert len(msg.message_id) == 64
    int(msg.message_id, 16)  # a valid hex digest
    assert msg.body_text.strip() == "hello world"


def test_parse_eml_accepts_bytes_str_path_and_pathlike(tmp_path):
    eml_path = _write_eml(tmp_path, "bytes_test.eml", _NO_MESSAGE_ID_EML.decode("utf-8"))
    from_bytes = parse_eml(_NO_MESSAGE_ID_EML)
    from_str_path = parse_eml(str(eml_path))
    from_pathlike = parse_eml(eml_path)
    assert from_bytes.body_text.strip() == from_str_path.body_text.strip() == from_pathlike.body_text.strip()


def test_parse_eml_preserves_raw_when_raw_dir_given(tmp_path):
    raw_dir = tmp_path / "raw"
    eml_path = _write_eml(tmp_path, "raw_test.eml", _REPLY_EML)
    msg = parse_eml(eml_path, raw_dir=raw_dir)

    assert msg.raw_ref is not None
    saved = Path(msg.raw_ref)
    assert saved.exists()
    assert saved.read_bytes() == eml_path.read_bytes()
    # message_id contains "@" which is not filesystem-safe; must be sanitized.
    assert "@" not in saved.name


def test_from_text_builds_message_with_stable_hash_id_and_urls():
    text = "Check this out: https://example.com/listing/42"
    msg = from_text(text, sender="+15551234567", channel=Channel.TELEGRAM)
    assert msg.channel == Channel.TELEGRAM
    assert msg.sender == "+15551234567"
    assert msg.body_text == text
    assert msg.urls == ["https://example.com/listing/42"]
    assert msg.message_id.startswith("txt_")

    # Same text -> same id when no explicit id given (stable hash).
    msg2 = from_text(text, sender="+15551234567", channel=Channel.TELEGRAM)
    assert msg2.message_id == msg.message_id


def test_from_text_respects_explicit_message_id_and_default_channel():
    msg = from_text("hello", message_id="custom-id-1")
    assert msg.message_id == "custom-id-1"
    assert msg.channel == Channel.MANUAL
