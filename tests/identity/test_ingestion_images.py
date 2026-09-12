"""Phase 2 (W10a): embedded/attached image extraction in ingestion/email.py.

A small PDF is generated on the fly with fpdf2 (a Pillow-generated bitmap embedded on each page)
so this test needs no binary fixtures.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from fpdf import FPDF
from PIL import Image

from dealsieve.ingestion.email import parse_eml

_LARGE_SIZE = (300, 250)  # >= 200x200: must survive the filter
_SMALL_SIZE = (50, 50)  # < 200x200: must be dropped as decoration


def _png_bytes(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", size, color=color)
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _build_pdf(*, with_images: bool) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(0, 10, "Page 1: large photo" if with_images else "Page 1: no photos here")
    if with_images:
        pdf.image(BytesIO(_png_bytes(_LARGE_SIZE, (200, 50, 50))), x=10, y=20, w=80)
        pdf.add_page()
        pdf.cell(0, 10, "Page 2: tiny icon")
        pdf.image(BytesIO(_png_bytes(_SMALL_SIZE, (0, 200, 0))), x=10, y=20, w=15)
    return bytes(pdf.output())


def _eml_with_attachments(*, pdf_bytes: bytes | None, image_bytes: bytes | None) -> bytes:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "Jane Broker <jane@brokerage.example>"
    msg["To"] = "buyer@example.com"
    msg["Subject"] = "Property condition report"
    msg["Message-ID"] = "<pca-test@brokerage.example>"
    msg.set_content("Attached is the inspection report.")
    if pdf_bytes is not None:
        msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename="report.pdf")
    if image_bytes is not None:
        msg.add_attachment(image_bytes, maintype="image", subtype="png", filename="roof_photo.png")
    return msg.as_bytes()


def test_pdf_images_extracted_filtered_by_size_and_stored_under_docs_dir(tmp_path, monkeypatch):
    docs_dir = tmp_path / "documents"
    monkeypatch.setenv("DEALSIEVE_DOCS_DIR", str(docs_dir))

    pdf_bytes = _build_pdf(with_images=True)
    eml_bytes = _eml_with_attachments(pdf_bytes=pdf_bytes, image_bytes=None)

    message = parse_eml(eml_bytes)
    assert len(message.attachments) == 1
    attachment = message.attachments[0]
    assert attachment.content_type == "application/pdf"

    # Only the >=200x200 image (page 1) survives; the 50x50 icon on page 2 is dropped.
    assert len(attachment.image_paths) == 1
    stored_path = Path(attachment.image_paths[0])
    assert stored_path.name == "img_1.png"
    assert stored_path.parent == docs_dir / attachment.sha256
    assert stored_path.exists()

    with Image.open(stored_path) as im:
        assert im.size == _LARGE_SIZE
        assert im.format == "PNG"


def test_pdf_with_no_images_parses_fine_with_empty_image_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("DEALSIEVE_DOCS_DIR", str(tmp_path / "documents"))
    pdf_bytes = _build_pdf(with_images=False)
    eml_bytes = _eml_with_attachments(pdf_bytes=pdf_bytes, image_bytes=None)

    message = parse_eml(eml_bytes)
    attachment = message.attachments[0]
    assert attachment.image_paths == []
    assert attachment.text is not None and "no photos here" in attachment.text.lower()


def test_broken_pdf_attachment_does_not_fail_parsing(tmp_path, monkeypatch):
    monkeypatch.setenv("DEALSIEVE_DOCS_DIR", str(tmp_path / "documents"))
    eml_bytes = _eml_with_attachments(pdf_bytes=b"this is not a real pdf file", image_bytes=None)

    message = parse_eml(eml_bytes)
    attachment = message.attachments[0]
    assert attachment.image_paths == []
    assert attachment.text is None  # pypdf cannot read it; the existing text path already tolerates this


def test_standalone_image_attachment_is_stored_as_one_image_regardless_of_size(tmp_path, monkeypatch):
    docs_dir = tmp_path / "documents"
    monkeypatch.setenv("DEALSIEVE_DOCS_DIR", str(docs_dir))

    # Deliberately smaller than the PDF-embedded-image threshold: the size filter is PDF-only.
    small_image_bytes = _png_bytes((80, 60), (10, 20, 30))
    eml_bytes = _eml_with_attachments(pdf_bytes=None, image_bytes=small_image_bytes)

    message = parse_eml(eml_bytes)
    assert len(message.attachments) == 1
    attachment = message.attachments[0]
    assert attachment.content_type == "image/png"
    assert len(attachment.image_paths) == 1

    stored_path = Path(attachment.image_paths[0])
    assert stored_path.name == "img_1.png"
    assert stored_path.parent == docs_dir / attachment.sha256
    with Image.open(stored_path) as im:
        assert im.size == (80, 60)


def test_broken_image_attachment_does_not_fail_parsing(tmp_path, monkeypatch):
    monkeypatch.setenv("DEALSIEVE_DOCS_DIR", str(tmp_path / "documents"))
    eml_bytes = _eml_with_attachments(pdf_bytes=None, image_bytes=b"not actually an image")

    message = parse_eml(eml_bytes)
    attachment = message.attachments[0]
    assert attachment.image_paths == []


def test_pdf_and_image_extraction_does_not_disturb_existing_text_extraction(tmp_path, monkeypatch):
    """Sanity check: adding image handling must not regress the pre-existing text/PDF path."""
    monkeypatch.setenv("DEALSIEVE_DOCS_DIR", str(tmp_path / "documents"))
    pdf_bytes = _build_pdf(with_images=True)
    eml_bytes = _eml_with_attachments(pdf_bytes=pdf_bytes, image_bytes=None)

    message = parse_eml(eml_bytes)
    attachment = message.attachments[0]
    assert attachment.text is not None
    assert "large photo" in attachment.text.lower()
    assert len(attachment.sha256) == 64
