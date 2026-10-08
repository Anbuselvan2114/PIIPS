"""
PDF export for the Reports menu (Super Admin only - see app.py's
/api/reports/{key}/export/pdf and database.run_report).

Kept separate from excel_export.py since that module is specifically about
the PIIPS invoice workbook (Purchase Header/Line/Reservation Entry sheets,
field mapping, templates) - a report's PDF has nothing to do with any of
that, just a plain columns/rows table.
"""

import datetime
import os
import re
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

_HEADER_BG = colors.HexColor("#14213D")
_ROW_ALT_BG = colors.HexColor("#F3F4F1")
_BORDER = colors.HexColor("#E3E6E4")


def _prettify_column(name):
    """"InvoiceDetails" -> "Invoice Details" - same rule as Reports.jsx's
    own prettifyColumn, kept in sync by hand since one's JS and the other's
    Python. Only affects the header row shown in the PDF; the underlying
    column KEY (what /run and the frontend's DataTable key off) is
    untouched."""
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return name.replace("_", " ").strip()


def _cell_text(value):
    """Plain-string form of one report cell's VALUE (dates/numbers/None -
    text) - still needs escaping and wrapping in a Paragraph before it's a
    real table cell, see _cell_paragraph. Unicode is avoided deliberately
    (not just here but throughout): reportlab's base-14 fonts (Helvetica)
    have no glyph for characters outside WinAnsi, so anything beyond plain
    ASCII in a report value would silently fail to render."""
    if value is None:
        return ""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.strftime("%Y-%m-%d %H:%M:%S" if isinstance(value, datetime.datetime) else "%Y-%m-%d")
    return str(value)


def _cell_paragraph(value, style):
    """A table cell as a wrapped Paragraph, not a bare string - a plain
    string in a reportlab Table is never wrapped at all, so anything
    longer than its column (a batch name, a status, a timestamp) bleeds
    straight across the cell border into its neighbours instead of
    growing the row taller. escape() guards against a value that happens
    to contain '&'/'<'/'>' (a Paragraph's text is parsed as mini-markup).
    Composite report columns (e.g. the SLA report's "Invoice Details")
    pack several labeled sub-values into one cell with CHAR(13)+CHAR(10)
    between them (see database.py's usp_Report_InvoiceStageSLA) - a raw
    '\\n' is just whitespace to a Paragraph, so it must become an explicit
    '<br/>' tag, done AFTER escaping so the literal '<'/'>' of the tag
    itself isn't escaped away too."""
    text = escape(_cell_text(value)).replace("\r\n", "\n").replace("\n", "<br/>")
    return Paragraph(text, style)


# A report at or under this many columns reads fine as a normal upright
# page; past it, the columns genuinely need landscape's extra width to
# avoid squeezing every cell unreadably narrow (see _PAGE_MARGIN/
# write_report_pdf - this is the one knob that decides portrait vs.
# landscape, not a fixed per-report choice).
_PORTRAIT_MAX_COLUMNS = 6
_PAGE_MARGIN = 24


def _page_size_for(num_columns):
    """Portrait for a narrow report, landscape for a wide one."""
    return A4 if num_columns <= _PORTRAIT_MAX_COLUMNS else landscape(A4)


def _font_size_for(num_columns):
    """Shrinks as columns pile up so a wide report (e.g. a 20-column
    invoice-wise SLA report) still fits its own page width instead of
    silently overflowing it - landscape alone isn't enough past ~15
    columns at a normal reading size."""
    if num_columns <= 8:
        return 8
    if num_columns <= 14:
        return 7
    if num_columns <= 20:
        return 6
    return 5


def write_report_pdf(columns, rows, out_path, title="Report"):
    """Write a PDF table straight from a report's generic {columns, rows}
    shape (see database.run_report) - portrait or landscape, and the font
    size, both chosen from the column count (see _page_size_for/
    _font_size_for) so a narrow report isn't needlessly wide and a wide
    one doesn't overflow its own page. Columns share the page width
    evenly; header row repeats if the table spans more than one page.
    Sample: write_report_pdf(['A'], [[1], [2]], 'out.pdf', 'My Report')"""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    page_size = _page_size_for(len(columns))
    font_size = _font_size_for(len(columns))
    doc = SimpleDocTemplate(out_path, pagesize=page_size,
                             leftMargin=_PAGE_MARGIN, rightMargin=_PAGE_MARGIN,
                             topMargin=_PAGE_MARGIN, bottomMargin=_PAGE_MARGIN)
    styles = getSampleStyleSheet()

    available_width = page_size[0] - 2 * _PAGE_MARGIN
    col_width = available_width / len(columns) if columns else available_width

    # Separate header/body styles (header is bold + white, to sit on
    # _HEADER_BG) - both wrap at col_width minus the cell's own padding,
    # which is exactly what lets a long value grow its row taller instead
    # of overflowing sideways into the next column.
    header_style = ParagraphStyle("ReportHeader", fontName="Helvetica-Bold",
                                   fontSize=font_size, leading=font_size + 2,
                                   textColor=colors.white)
    body_style = ParagraphStyle("ReportBody", fontName="Helvetica",
                                 fontSize=font_size, leading=font_size + 2)

    data = ([[_cell_paragraph(_prettify_column(c), header_style) for c in columns]]
            + [[_cell_paragraph(v, body_style) for v in row] for row in rows])
    table = Table(data, colWidths=[col_width] * len(columns), repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ROW_ALT_BG]),
        ("GRID", (0, 0), (-1, -1), 0.5, _BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    doc.build([Paragraph(escape(_cell_text(title)), styles["Title"]), Spacer(1, 12), table])
    return out_path
