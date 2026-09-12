#!/usr/bin/env python3
"""Builds `fixtures/om/05_power_inn_property_condition_report.pdf` for W11 (Act 3 fixtures).

A deterministic, real-looking property condition assessment PDF: cover page, executive summary,
a roof section (with two embedded photos), an HVAC section (with one embedded photo and a unit
table), brief electrical/plumbing/paving sections, and a findings summary table with page numbers.

Content follows `docs/CONTRACTS.md` "W11: fixtures for Act 3" item 2 exactly: it must NOT mention
Phase I / environmental or CAM (those stay open diligence items chased separately).

Usage:
    source .venv/bin/activate
    python scripts/build_fixture_pdfs.py

Deterministic: fixed creation date, fixed text, fixed image files -> byte-identical output run to run
(aside from fpdf2's internal object ordering, which is itself deterministic for a fixed input).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF

REPO_ROOT = Path(__file__).resolve().parent.parent
PHOTOS_DIR = REPO_ROOT / "fixtures" / "photos"
OUTPUT_PATH = REPO_ROOT / "fixtures" / "om" / "05_power_inn_property_condition_report.pdf"

ADDRESS = "8330 Power Inn Road, Sacramento, CA 95826"
SITE_VISIT_DATE = "2026-06-18"
REPORT_DATE = "2026-06-25"
FIRM_NAME = "Delta Building Consultants, Inc."
CREATION_DATE = datetime(2026, 6, 25, 9, 0, 0, tzinfo=UTC)

PAGE_WIDTH_MM = 210.0
MARGIN_MM = 20.0
CONTENT_WIDTH_MM = PAGE_WIDTH_MM - 2 * MARGIN_MM


class ConditionReportPDF(FPDF):
    """FPDF subclass with a running header/footer, skipped on the cover page."""

    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_y(10)
        self.set_font("helvetica", "", 8)
        self.set_text_color(90, 90, 90)
        self.cell(CONTENT_WIDTH_MM * 0.7, 5, "Property Condition Assessment - 8330 Power Inn Road, Sacramento, CA")
        self.cell(CONTENT_WIDTH_MM * 0.3, 5, SITE_VISIT_DATE, align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(180, 180, 180)
        self.set_line_width(0.2)
        self.line(MARGIN_MM, 16, PAGE_WIDTH_MM - MARGIN_MM, 16)
        self.set_text_color(0, 0, 0)
        self.set_y(21)

    def footer(self) -> None:
        if self.page_no() == 1:
            return
        self.set_y(-15)
        self.set_font("helvetica", "", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 10, f"Page {self.page_no()} of {{nb}}", align="C")
        self.set_text_color(0, 0, 0)

    # -- content helpers -----------------------------------------------------------------

    def section_title(self, number: str, text: str) -> None:
        self.set_font("helvetica", "B", 13)
        self.set_text_color(20, 20, 20)
        self.cell(0, 9, f"{number}. {text}", new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(20, 20, 20)
        self.set_line_width(0.4)
        self.line(self.get_x(), self.get_y(), self.get_x() + CONTENT_WIDTH_MM, self.get_y())
        self.ln(4)
        self.set_text_color(0, 0, 0)

    def subsection_title(self, text: str) -> None:
        self.set_font("helvetica", "B", 11)
        self.cell(0, 7, text, new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def body_text(self, text: str, size: int = 10) -> None:
        self.set_font("helvetica", "", size)
        self.multi_cell(CONTENT_WIDTH_MM, 5.5, text)
        self.ln(2)

    def caption(self, text: str) -> None:
        self.set_font("helvetica", "I", 9)
        self.set_text_color(60, 60, 60)
        self.multi_cell(CONTENT_WIDTH_MM, 5, text)
        self.set_text_color(0, 0, 0)
        self.ln(3)

    def photo(self, path: Path, width_mm: float = 130.0) -> None:
        x = MARGIN_MM + (CONTENT_WIDTH_MM - width_mm) / 2
        self.image(str(path), x=x, w=width_mm)
        self.ln(3)

    def simple_table(
        self,
        headers: list[str],
        rows: list[list[str]],
        col_widths: list[float],
        row_height: float = 7.0,
        header_size: int = 9,
        body_size: int = 9,
    ) -> None:
        self.set_font("helvetica", "B", header_size)
        self.set_fill_color(230, 230, 230)
        self.set_draw_color(140, 140, 140)
        self.set_line_width(0.2)
        for header, width in zip(headers, col_widths, strict=True):
            self.cell(width, row_height, header, border=1, align="L", fill=True)
        self.ln(row_height)
        self.set_font("helvetica", "", body_size)
        for row_index, row in enumerate(rows):
            fill = row_index % 2 == 1
            if fill:
                self.set_fill_color(245, 245, 245)
            for value, width in zip(row, col_widths, strict=True):
                self.cell(width, row_height, value, border=1, align="L", fill=fill)
            self.ln(row_height)
        self.ln(3)


def build() -> Path:
    pdf = ConditionReportPDF(orientation="P", unit="mm", format="A4")
    pdf.set_creation_date(CREATION_DATE)
    pdf.set_title("Property Condition Assessment - 8330 Power Inn Road")
    pdf.set_author(FIRM_NAME)
    pdf.set_subject("Property condition assessment")
    pdf.set_creator("dealsieve fixtures/build_fixture_pdfs.py")
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_margins(MARGIN_MM, MARGIN_MM, MARGIN_MM)

    # --- Page 1: cover ---------------------------------------------------------------
    pdf.add_page()
    pdf.set_y(70)
    pdf.set_font("helvetica", "B", 22)
    pdf.multi_cell(CONTENT_WIDTH_MM, 11, "Property Condition Assessment", align="C")
    pdf.ln(6)
    pdf.set_font("helvetica", "", 14)
    pdf.multi_cell(CONTENT_WIDTH_MM, 8, ADDRESS, align="C")
    pdf.ln(14)
    pdf.set_font("helvetica", "", 11)
    pdf.multi_cell(CONTENT_WIDTH_MM, 6.5, f"Site visit: {SITE_VISIT_DATE}", align="C")
    pdf.multi_cell(CONTENT_WIDTH_MM, 6.5, "Prepared for the owner", align="C")
    pdf.ln(20)
    pdf.set_font("helvetica", "", 10)
    pdf.set_text_color(90, 90, 90)
    pdf.multi_cell(CONTENT_WIDTH_MM, 6, FIRM_NAME, align="C")
    pdf.multi_cell(CONTENT_WIDTH_MM, 6, f"Report date: {REPORT_DATE}", align="C")
    pdf.set_text_color(0, 0, 0)

    # --- Page 2: executive summary -----------------------------------------------------
    pdf.add_page()
    pdf.section_title("1", "Executive Summary")
    pdf.body_text(
        "This report presents the findings of a property condition assessment of the subject "
        f"property at {ADDRESS}, an approximately 20,000-square-foot, eight-suite small-bay "
        f"industrial building. The site visit was conducted on {SITE_VISIT_DATE}. The assessment "
        "covers the roof, HVAC equipment, electrical service, plumbing, and paved areas."
    )
    pdf.body_text(
        "The roof is the original built-up membrane installed in 2001, with no documented "
        "replacement since. Ponding water and membrane blistering were observed; replacement is "
        "recommended within 12 to 24 months at a budget of $85,000 to $95,000."
    )
    pdf.body_text(
        "Eight packaged rooftop HVAC units serve the building. Six units, installed between 2014 "
        "and 2019, are in good working order. Two units, serving suites 103 and 106, date to 1998 "
        "and are beyond typical service life; both were operational at the time of inspection."
    )
    pdf.body_text(
        "Electrical and plumbing systems were found to be serviceable with no immediate deficiencies. "
        "The asphalt paving shows typical wear and would benefit from a seal coat and restriping "
        "within 3 to 5 years."
    )
    pdf.body_text(
        "Cost ranges and timeframes for all items identified in this assessment are summarized in "
        "the findings table in Section 6."
    )

    # --- Page 3: roof assessment, part 1 ------------------------------------------------
    pdf.add_page()
    pdf.section_title("2", "Roof Assessment")
    pdf.body_text(
        "The roof is a low-slope built-up roof (BUR) membrane, gravel-surfaced, covering the "
        "full building footprint. Roofing permit and vendor records on file with the owner "
        "indicate the membrane is original to the building's 2001 construction; no documented "
        "replacement, recover, or full re-roof has occurred since that date. The membrane is "
        "well past its typical 20-year service life for this roof type."
    )
    pdf.body_text(
        "Ponding water was observed at the northeast corner of the roof during the site visit, "
        "consistent with a drainage deficiency in that area. Ponding accelerates membrane "
        "deterioration and should be corrected as part of any re-roofing scope."
    )
    pdf.photo(PHOTOS_DIR / "roof_ponding.jpg")
    pdf.caption("Photo 1: ponding water, NE corner, approx. 1/2 inch after 48 dry hours")

    # --- Page 4: roof assessment, part 2 ------------------------------------------------
    pdf.add_page()
    pdf.body_text(
        "In addition to the ponding condition, the membrane surface shows widespread blistering "
        "and at least one open seam, observed near the suite 105 HVAC curb. Blistering indicates "
        "trapped moisture beneath the membrane and, combined with an open seam, creates an active "
        "leak risk at that location."
    )
    pdf.photo(PHOTOS_DIR / "roof_membrane.jpg")
    pdf.caption("Photo 2: membrane blistering and open seam near suite 105 HVAC curb")
    pdf.subsection_title("Recommendation")
    pdf.body_text(
        "Given the roof's age (original to 2001, no documented replacement), the observed ponding, "
        "and the membrane blistering and open seam, we recommend full roof replacement within 12 "
        "to 24 months. Budget $85,000 to $95,000 for a like-for-like built-up or single-ply "
        "replacement, including correction of the NE corner drainage deficiency."
    )

    # --- Page 5: HVAC assessment, part 1 ------------------------------------------------
    pdf.add_page()
    pdf.section_title("3", "HVAC Assessment")
    pdf.body_text(
        "Eight rooftop packaged HVAC units serve the building, one per suite. Unit condition was "
        "assessed visually and by nameplate data where accessible; no units were opened for "
        "internal inspection. All eight units were operational at the time of the site visit."
    )
    pdf.photo(PHOTOS_DIR / "rooftop_hvac.jpg")
    pdf.caption("Photo 3: rooftop packaged HVAC units")
    pdf.simple_table(
        headers=["Unit", "Suite", "Install year", "Condition / notes"],
        rows=[
            ["1", "101", "2016", "Operational; routine maintenance"],
            ["2", "102", "2015", "Operational; routine maintenance"],
            ["3", "103", "1998", "Beyond typical service life; operational at inspection"],
            ["4", "104", "2017", "Operational; routine maintenance"],
            ["5", "105", "2014", "Operational; routine maintenance"],
            ["6", "106", "1998", "Beyond typical service life; operational at inspection"],
            ["7", "107", "2018", "Operational; routine maintenance"],
            ["8", "108", "2019", "Operational; routine maintenance"],
        ],
        col_widths=[15, 20, 28, 107],
    )

    # --- Page 6: HVAC (cont.), electrical, plumbing, paving -----------------------------
    pdf.add_page()
    pdf.body_text(
        "The units serving suites 103 and 106 are original 1998 packaged units and are beyond "
        "typical service life (typical service life for packaged rooftop units is 15 to 20 years). "
        "Both units were operational at the time of inspection with no immediate signs of failure. "
        "Budget replacement of these two units within 3 to 5 years, at $14,000 to $18,000 each."
    )
    pdf.section_title("4", "Electrical")
    pdf.body_text(
        "Electrical service appears original to the building. Panels are labeled and accessible in "
        "each suite. No exposed wiring, overheating, or other deficiencies were observed. No "
        "immediate capital needs are identified for the electrical system."
    )
    pdf.section_title("5", "Plumbing")
    pdf.body_text(
        "Plumbing fixtures and visible supply and waste lines were serviceable at the time of "
        "inspection. No active leaks, corrosion, or deficiencies were noted in the suites accessed. "
        "No immediate capital needs are identified for the plumbing system."
    )
    pdf.section_title("6", "Paving")
    pdf.body_text(
        "The asphalt parking and loading areas show typical wear, including surface cracking and "
        "faded striping. No structural (base) failures were observed. Recommend a seal coat and "
        "restripe within 3 to 5 years, budget $6,000 to $8,000."
    )

    # --- Page 7: summary of findings -----------------------------------------------------
    pdf.add_page()
    pdf.section_title("7", "Summary of Findings")
    pdf.body_text(
        "The table below summarizes the capital items identified in this assessment, with cost "
        "ranges and recommended timeframes."
    )
    pdf.simple_table(
        headers=["Item", "Cost range", "Timeframe"],
        rows=[
            ["Roof replacement (original 2001 BUR membrane)", "$85,000 - $95,000", "12 - 24 months"],
            ["HVAC replacement, suites 103 & 106 (1998 units, $14k-$18k each)", "$28,000 - $36,000", "3 - 5 years"],
            ["Electrical", "None identified", "N/A"],
            ["Plumbing", "None identified", "N/A"],
            ["Paving seal coat and restripe", "$6,000 - $8,000", "3 - 5 years"],
        ],
        col_widths=[100, 45, 25],
        row_height=8.0,
    )
    pdf.ln(2)
    pdf.body_text(
        "This report reflects physical conditions observed on the date of the site visit only, "
        "limited to the roof, HVAC, electrical, plumbing, and paving scope described above.",
        size=9,
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(OUTPUT_PATH))
    return OUTPUT_PATH


if __name__ == "__main__":
    path = build()
    print(f"wrote {path} ({path.stat().st_size} bytes)")
