"""PDF report generation for product-label scans."""

from html import escape
from io import BytesIO

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from scanner.models import ExtractedField, Report, RuleResult, Scan
from scanner.services.assessment import assessment_from_results
from scanner.services.field_labels import field_label


BLUE = colors.HexColor("#174ea6")
PALE_BLUE = colors.HexColor("#eaf1fb")
GRID = colors.HexColor("#d8dee9")
AMBER = colors.HexColor("#9a6700")
RED = colors.HexColor("#b42318")
GREEN = colors.HexColor("#198754")
GREY = colors.HexColor("#667085")
PASS_BG = colors.HexColor("#e8f7ef")
FAIL_BG = colors.HexColor("#fff0f1")
REVIEW_BG = colors.HexColor("#fff7df")
NA_BG = colors.HexColor("#f1f3f6")


def _paragraph(value, style):
    safe_text = escape(str(value or "—")).replace("\n", "<br/>")
    return Paragraph(safe_text, style)


def _table_style(header=True, status_rows=()):
    commands = [
        ("GRID", (0, 0), (-1, -1), 0.35, GRID),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    if header:
        commands.extend([
            ("BACKGROUND", (0, 0), (-1, 0), PALE_BLUE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ])
    for row, color, background in status_rows:
        commands.extend([
            ("BACKGROUND", (2, row), (2, row), background),
            ("TEXTCOLOR", (2, row), (2, row), color),
            ("FONTNAME", (2, row), (2, row), "Helvetica-Bold"),
            ("ALIGN", (2, row), (2, row), "CENTER"),
        ])
    return TableStyle(commands)


def _draw_page_number(canvas, document):
    canvas.saveState()
    canvas.setStrokeColor(GRID)
    canvas.setLineWidth(0.5)
    canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(colors.HexColor("#667085"))
    canvas.drawString(18 * mm, 9 * mm, "Automated screening only, not legal advice.")
    canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"Page {document.page}")
    canvas.restoreState()


def _rule_status(result):
    if result.status == RuleResult.Status.PASS:
        return "PASS", GREEN, PASS_BG
    if result.status == RuleResult.Status.FAIL:
        return "FAIL", RED, FAIL_BG
    if result.status == RuleResult.Status.NOT_APPLICABLE:
        return "N/A", GREY, NA_BG
    return "REVIEW", AMBER, REVIEW_BG


FIELD_GROUPS = (
    ("Product Details", {"brand", "generic_name", "barcode", "batch_number", "fssai_license", "country_of_origin"}),
    ("Manufacturer Details", {"manufacturer", "manufacturer_address", "marketer", "packed_by", "imported_by", "manufacturer_pin", "marketer_pin", "packed_by_pin", "imported_by_pin"}),
    ("Pricing", {"mrp", "mrp_currency", "mrp_inclusive_of_taxes", "unit_sale_price"}),
    ("Quantity", {"net_quantity", "net_quantity_conflict", "net_quantities", "net_quantity_front", "net_quantity_back"}),
    ("Dates", {"mfg_date", "pkd_date", "packed_on", "expiry_date"}),
    ("Consumer Care", {"consumer_care", "phone", "email"}),
)


def _short_reference(value):
    """Keep only the concise rule/citation portion of an editable legal reference."""
    import re

    match = re.search(r"Rule\s+[\w()./-]+", value or "", re.IGNORECASE)
    return match.group(0) if match else (value or "—")


def _field_source(field):
    labels = []
    if field.source_type == "manual":
        labels.append("Entered manually")
    if field.source_image_id:
        labels.append(f"{field.source_image.get_side_display()} image")
    elif field.source_type != "manual":
        labels.append("OCR")
    if field.source_pass:
        labels.append(f"Pass: {field.source_pass}")
    return " · ".join(labels) or "Unavailable"


def generate_pdf(scan):
    """Build a scan report, save it to ``Report``, and return its PDF bytes."""
    from time import perf_counter
    import logging

    started = perf_counter()
    if not isinstance(scan, Scan):
        scan = Scan.objects.select_related("user", "product").get(pk=scan)
    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm,
        topMargin=15 * mm, bottomMargin=22 * mm, title=f"Label scan report {scan.pk}",
        author="LabelLens",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ReportTitle", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=17, leading=20, textColor=colors.white, alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="SmallCell", parent=styles["Normal"], fontName="Helvetica", fontSize=8, leading=10, textColor=colors.HexColor("#26364d")))
    styles.add(ParagraphStyle(name="Section", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=BLUE, spaceBefore=9, spaceAfter=5))
    styles.add(ParagraphStyle(name="ScoreLabel", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=BLUE))
    styles.add(ParagraphStyle(name="ScoreValue", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=16, leading=18, textColor=BLUE))
    styles.add(ParagraphStyle(name="ScoreVerdict", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=10, leading=12, textColor=BLUE))

    story = [
        Table([[Paragraph("LabelLens  ·  Legal Metrology Label Scan Report", styles["ReportTitle"])]], colWidths=[174 * mm], style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), BLUE),
            ("BOX", (0, 0), (-1, -1), 0, BLUE),
            ("TOPPADDING", (0, 0), (-1, -1), 11),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
        ])),
        Spacer(1, 3 * mm),
    ]
    created = timezone.localtime(scan.created_at) if timezone.is_aware(scan.created_at) else scan.created_at
    product_name = scan.product.name if scan.product_id else "Not identified"
    brand = scan.product.brand if scan.product_id and scan.product.brand else "Not identified"
    # A scan passed from the result view can have prefetched, now-stale related
    # rows after manual correction and reassessment. Read the persisted state.
    assessment = assessment_from_results(RuleResult.objects.filter(scan_id=scan.pk))
    score = assessment["display"]
    confidence = f"{scan.ocr_confidence:.0f}%" if scan.ocr_confidence is not None else "Unavailable"
    score_box = Table([[
        [Paragraph("OVERALL SCORE", styles["ScoreLabel"]), Spacer(1, 1 * mm), Paragraph(score, styles["ScoreValue"])],
        [Paragraph("VERDICT", styles["ScoreLabel"]), Spacer(1, 1 * mm), Paragraph(assessment["verdict_label"], styles["ScoreVerdict"])],
    ]], colWidths=[87 * mm, 87 * mm])
    score_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PALE_BLUE),
        ("BOX", (0, 0), (-1, -1), 0.8, BLUE),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([score_box, Spacer(1, 4 * mm)])
    metadata = [
        ["Scan", f"#{scan.pk}"],
        ["Date", created.strftime("%d %b %Y, %H:%M")],
        ["User", scan.user.get_full_name() or scan.user.get_username()],
        ["Product", product_name],
        ["Brand", brand],
        ["OCR confidence", confidence],
        ["Overall score", score],
        ["Assessed rules", f"{assessment['assessed']} of {assessment['applicable']}"],
        ["Rule counts", f"PASS {assessment['passed']} | FAIL {assessment['failed']} | NEEDS REVIEW {assessment['needs_review']} | N/A {assessment['not_applicable']}"],
        ["Rules needing review", str(assessment["needs_review"])],
        ["Verdict", assessment["verdict_label"]],
        ["Verdict details", assessment["details_label"]],
    ]
    meta_table = Table(metadata, colWidths=[37 * mm, 135 * mm])
    meta_table.setStyle(_table_style(header=False))
    meta_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), PALE_BLUE),
        ("TEXTCOLOR", (0, 0), (0, -1), BLUE),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("TEXTCOLOR", (1, 0), (-1, -1), colors.HexColor("#26364d")),
    ]))
    story.extend([meta_table, Spacer(1, 3 * mm)])

    images = list(scan.images.all())
    if images:
        story.append(Paragraph("Uploaded label images", styles["Section"]))
        for scan_image in images:
            try:
                image_content = Image(scan_image.image.path, width=75 * mm, height=52 * mm, kind="proportional")
            except Exception:
                image_content = _paragraph("This image could not be included in the report.", styles["SmallCell"])
            story.append(KeepTogether([
                _paragraph(f"{scan_image.get_side_display()} label image", styles["SmallCell"]),
                image_content,
                Spacer(1, 2 * mm),
            ]))

    story.append(Paragraph("Extracted fields", styles["Section"]))
    minimum_confidence = getattr(settings, "OCR_MIN_CONFIDENCE", 40)
    fields = list(ExtractedField.objects.filter(scan_id=scan.pk).select_related("source_image"))
    for group, names in FIELD_GROUPS:
        group_fields = [field for field in fields if field.field_name in names]
        if not group_fields:
            continue
        story.append(Paragraph(group, styles["ScoreLabel"]))
        field_rows = [[_paragraph("Field", styles["SmallCell"]), _paragraph("Value", styles["SmallCell"]), _paragraph("Confidence", styles["SmallCell"]), _paragraph("Read status", styles["SmallCell"]), _paragraph("Source", styles["SmallCell"])]]
        for field in group_fields:
            field_confidence = f"{field.confidence:.0f}%" if field.confidence is not None else "Unavailable"
            uncertain = bool(field.review_reason) or field.confidence is None or field.confidence < minimum_confidence
            details = [field.value]
            if field.original_evidence:
                details.append(f"Evidence: {field.original_evidence}")
            if field.review_reason:
                details.append(f"Review: {field.review_reason}")
            if field.alternatives:
                details.append("Alternatives: " + ", ".join(field.alternatives))
            if field.original_value and field.original_value != field.value:
                details.append(f"Original OCR: {field.original_value}")
                if field.original_evidence:
                    details.append(f"Original evidence: {field.original_evidence}")
            field_rows.append([
                _paragraph(field_label(field.field_name), styles["SmallCell"]),
                _paragraph("\n".join(details), styles["SmallCell"]),
                _paragraph(field_confidence, styles["SmallCell"]),
                _paragraph("Uncertain" if uncertain else "Read", styles["SmallCell"]),
                _paragraph(_field_source(field), styles["SmallCell"]),
            ])
        fields_table = Table(field_rows, colWidths=[31 * mm, 66 * mm, 23 * mm, 25 * mm, 29 * mm], repeatRows=1)
        fields_table.setStyle(_table_style())
        story.extend([fields_table, Spacer(1, 2 * mm)])
    known_names = set().union(*(names for _label, names in FIELD_GROUPS))
    other_fields = [field for field in fields if field.field_name not in known_names]
    if other_fields:
        story.append(Paragraph("Other extracted fields", styles["ScoreLabel"]))
        field_rows = [[_paragraph("Field", styles["SmallCell"]), _paragraph("Value", styles["SmallCell"]), _paragraph("Confidence", styles["SmallCell"]), _paragraph("Read status", styles["SmallCell"]), _paragraph("Source", styles["SmallCell"])]]
        for field in other_fields:
            uncertain = bool(field.review_reason) or field.confidence is None or field.confidence < minimum_confidence
            details = [field.value]
            if field.original_evidence:
                details.append(f"Evidence: {field.original_evidence}")
            if field.review_reason:
                details.append(f"Review: {field.review_reason}")
            if field.alternatives:
                details.append("Alternatives: " + ", ".join(field.alternatives))
            if field.original_value and field.original_value != field.value:
                details.extend((f"Original OCR: {field.original_value}", field.original_evidence))
            field_rows.append([_paragraph(field_label(field.field_name), styles["SmallCell"]), _paragraph("\n".join(filter(None, details)), styles["SmallCell"]), _paragraph(f"{field.confidence:.0f}%" if field.confidence is not None else "Unavailable", styles["SmallCell"]), _paragraph("Uncertain" if uncertain else "Read", styles["SmallCell"]), _paragraph(_field_source(field), styles["SmallCell"])])
        table = Table(field_rows, colWidths=[31 * mm, 66 * mm, 23 * mm, 25 * mm, 29 * mm], repeatRows=1)
        table.setStyle(_table_style())
        story.append(table)
    if not fields:
        story.append(_paragraph("No text was extracted from the uploaded images.", styles["SmallCell"]))

    story.append(Paragraph("Rule checks", styles["Section"]))
    rule_rows = [[
        _paragraph("Rule", styles["SmallCell"]),
        _paragraph("Legal reference", styles["SmallCell"]),
        _paragraph("Status", styles["SmallCell"]),
        _paragraph("Detected value", styles["SmallCell"]),
        _paragraph("Evidence", styles["SmallCell"]),
        _paragraph("Suggested fix", styles["SmallCell"]),
    ]]
    status_rows = []
    review_items = []
    results = list(RuleResult.objects.filter(scan_id=scan.pk).select_related("rule", "source_image"))
    for row, result in enumerate(results, start=1):
        display_status, status_color, status_background = _rule_status(result)
        review_reason = (result.message or "This declaration could not be verified from the available label text.") if result.status == RuleResult.Status.NEEDS_REVIEW else ""
        rule_rows.append([
            _paragraph(result.rule.name, styles["SmallCell"]),
            _paragraph(_short_reference(result.legal_reference or result.rule.legal_reference), styles["SmallCell"]),
            _paragraph(display_status, styles["SmallCell"]),
            _paragraph(result.found_value, styles["SmallCell"]),
            _paragraph("\n".join(filter(None, (
                f"Source image: {result.source_image.get_side_display()}" if result.source_image_id else "",
                "Entered manually" if result.source_type == RuleResult.SourceType.MANUAL else "",
                f"OCR pass: {result.source_pass}" if result.source_pass else "",
                result.evidence,
                f"Review reason: {review_reason}" if review_reason else "",
                f"Original OCR value: {result.original_ocr_value}" if result.original_ocr_value else "",
                result.original_ocr_evidence if result.original_ocr_evidence else "",
            ))), styles["SmallCell"]),
            _paragraph(result.suggested_fix, styles["SmallCell"]),
        ])
        status_rows.append((row, status_color, status_background))
        if result.status == RuleResult.Status.NEEDS_REVIEW:
            review_items.append(f"{result.rule.name}: {review_reason}")
    if len(rule_rows) == 1:
        rule_rows.append([_paragraph("No rule results", styles["SmallCell"]), "", "", "", "", ""])
    rules_table = Table(rule_rows, colWidths=[29 * mm, 26 * mm, 22 * mm, 31 * mm, 38 * mm, 26 * mm], repeatRows=1)
    rules_table.setStyle(_table_style(status_rows=status_rows))
    story.append(rules_table)

    if scan.ocr_confidence is not None and scan.ocr_confidence < getattr(settings, "OCR_MIN_CONFIDENCE", 40):
        story.append(Paragraph("Needs review", styles["Section"]))
        story.append(_paragraph("Text could not be read reliably.", styles["SmallCell"]))
    if review_items:
        story.append(Paragraph("Needs review", styles["Section"]))
        story.append(_paragraph("\n".join(review_items), styles["SmallCell"]))

    document.build(story, onFirstPage=_draw_page_number, onLaterPages=_draw_page_number)
    pdf_bytes = output.getvalue()
    report, _ = Report.objects.get_or_create(scan=scan)
    previous_name = report.pdf_file.name if report.pdf_file else None
    report.pdf_file.save(f"scan-{scan.pk}-report.pdf", ContentFile(pdf_bytes), save=True)
    if previous_name and previous_name != report.pdf_file.name:
        report.pdf_file.storage.delete(previous_name)
    logging.getLogger(__name__).info("PDF generation scan=%s elapsed_seconds=%.3f", scan.pk, perf_counter() - started)
    return pdf_bytes
