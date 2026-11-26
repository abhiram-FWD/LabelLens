from datetime import timedelta
import logging
from types import SimpleNamespace

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.http import FileResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET
from django.utils import timezone
from django.utils.dateparse import parse_date

from .models import ExtractedField, Product, Report, RuleResult, Scan, ScanImage
from .services.pipeline import process_scan
from .services.report import generate_pdf
from .services.assessment import assessment_for_scan, assessment_from_results
from .services.field_labels import display_ocr_text, field_label
from .services.upload_validation import validate_uploaded_images

logger = logging.getLogger(__name__)


def _mark_scan_processing_error(scan):
    """Keep failed processing in review state and prevent stale violations."""
    scan.status = Scan.Status.NEEDS_REVIEW
    scan.score = None
    scan.ocr_confidence = 0
    scan.save(update_fields=("status", "score", "ocr_confidence"))
    scan.rule_results.update(
        status=RuleResult.Status.NEEDS_REVIEW,
        message="The scan could not be checked reliably. Review the label or try scanning it again.",
    )


def home(request):
    if request.user.is_authenticated:
        return redirect("scanner:dashboard")
    return render(request, "scanner/landing.html")


@login_required
def dashboard(request):
    status_choices = list(Scan.Status.choices)
    status_counts = {}
    for stored_scan in request.user.scans.prefetch_related("rule_results"):
        verdict = assessment_for_scan(stored_scan)["verdict"]
        status_counts[verdict] = status_counts.get(verdict, 0) + 1
    status_data = [{"label": label, "count": status_counts.get(value, 0)} for value, label in status_choices]

    result_counts = {
        item["status"]: item["total"]
        for item in RuleResult.objects.filter(scan__user=request.user)
        .values("status").annotate(total=Count("id"))
    }
    rule_result_data = [
        {"status": value, "label": label, "count": result_counts.get(value, 0)}
        for value, label in RuleResult.Status.choices
    ]

    verified_violations = RuleResult.objects.filter(
        scan__user=request.user, status=RuleResult.Status.FAIL,
    ).select_related("scan", "rule")
    failed_rules = list(
        verified_violations
        .values("rule__name").annotate(total=Count("id")).order_by("-total", "rule__name")[:8]
    )
    dashboard_assessment = assessment_from_results(
        RuleResult.objects.filter(scan__user=request.user).only("status")
    )
    recent_scans = list(request.user.scans.select_related("product").prefetch_related("images", "rule_results")[:6])
    for recent_scan in recent_scans:
        recent_scan.assessment = assessment_for_scan(recent_scan)
        recent_scan.score_display = recent_scan.assessment["display"]
        recent_scan.assessment_verdict_label = dict(Scan.Status.choices).get(recent_scan.assessment["verdict"], "Needs review")
    since = timezone.now() - timedelta(days=29)
    daily_scans = list(
        request.user.scans.filter(created_at__gte=since)
        .annotate(day=TruncDate("created_at")).values("day").annotate(total=Count("id")).order_by("day")
    )
    timeline = {entry["day"].isoformat(): entry["total"] for entry in daily_scans}
    days = [(timezone.localdate() - timedelta(days=offset)).isoformat() for offset in range(29, -1, -1)]
    context = {
        "status_data": status_data,
        "rule_result_data": rule_result_data,
        "rule_result_counts": result_counts,
        "total_rule_results": sum(result_counts.values()),
        "failed_check_count": result_counts.get(RuleResult.Status.FAIL, 0),
        "verified_violation_count": verified_violations.count(),
        "needs_review_rule_count": result_counts.get(RuleResult.Status.NEEDS_REVIEW, 0),
        "not_applicable_rule_count": result_counts.get(RuleResult.Status.NOT_APPLICABLE, 0),
        "assessed_rule_count": dashboard_assessment["assessed"],
        "compliance_rate": dashboard_assessment["score"],
        "compliance_label": dashboard_assessment["score_label"],
        "compliance_display": dashboard_assessment["display"],
        "review_rule_count": dashboard_assessment["needs_review"],
        "total_scans": sum(item["count"] for item in status_data),
        "recent_scans": recent_scans,
        "failed_rules": failed_rules,
        "chart_days": days,
        "chart_scan_counts": [timeline.get(day, 0) for day in days],
    }
    return render(request, "scanner/home.html", context)


@require_GET
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return JsonResponse({"status": "unhealthy", "database": "unavailable"}, status=503)
    return JsonResponse({"status": "ok", "database": "ok"})


@login_required
def upload(request):
    if request.method == "POST":
        side_uploads = [
            (ScanImage.Side.FRONT, request.FILES.getlist("front_image")),
            (ScanImage.Side.BACK, request.FILES.getlist("back_image")),
            (ScanImage.Side.OTHER, request.FILES.getlist("side_images")),
        ]
        uploads = [(side, image) for side, side_files in side_uploads for image in side_files]
        # Preserve support for older clients which still post a generic image list.
        uploads.extend((ScanImage.Side.OTHER, image) for image in request.FILES.getlist("images"))
        images = [image for _side, image in uploads]
        try:
            validate_uploaded_images(images)
        except ValidationError as exc:
            return render(request, "scanner/upload.html", {"error": " ".join(exc.messages), "errors": exc.messages}, status=400)
        scan = Scan.objects.create(user=request.user)
        for side, image in uploads:
            ScanImage.objects.create(scan=scan, side=side, image=image)
        try:
            process_scan(scan)
        except Exception:
            logger.exception("Scan processing failed for scan %s", scan.pk)
            _mark_scan_processing_error(scan)
            messages.warning(request, "The images were uploaded, but text extraction or rule checking could not be completed. Try clearer photos or review the details manually.")
        if not any(side == ScanImage.Side.BACK for side, _image in uploads):
            messages.warning(request, "No back image was uploaded. Some declarations may need manual review.")
        return redirect("scanner:result", scan_id=scan.pk)
    return render(request, "scanner/upload.html")


@login_required
def result(request, scan_id):
    scan = get_object_or_404(
        Scan.objects.select_related("product", "report").prefetch_related(
            "images", "extracted_fields__source_image", "rule_results__rule", "rule_results__source_image"
        ),
        pk=scan_id,
        user=request.user,
    )
    manual_fields = (
        ("mrp", "Maximum retail price"),
        ("net_quantity", "Net quantity"),
        ("mfg_date", "Manufacture date"),
        ("manufacturer", "Manufacturer address"),
        ("marketer", "Marketed by"),
        ("packed_by", "Packed by"),
        ("imported_by", "Imported by"),
        ("consumer_care", "Consumer care"),
        ("expiry_date", "Use by / best before date"),
        ("country_of_origin", "Country of origin"),
        ("generic_name", "Generic name"),
    )
    if request.method == "POST" and request.POST.get("action") == "edit_product":
        product_name = request.POST.get("product_name", "").strip()
        brand = request.POST.get("brand", "").strip()
        if not product_name:
            messages.error(request, "Enter a product name to save these details.")
            return redirect("scanner:result", scan_id=scan.pk)
        if scan.product_id:
            scan.product.name = product_name
            scan.product.brand = brand
            scan.product.save(update_fields=("name", "brand"))
        else:
            scan.product = Product.objects.create(name=product_name, brand=brand)
            scan.save(update_fields=("product",))
        messages.success(request, "Product details saved.")
        return redirect("scanner:result", scan_id=scan.pk)
    action = request.POST.get("action") if request.method == "POST" else ""
    if action in {"manual_verify", "recheck_fields"}:
        existing = list(scan.extracted_fields.all())
        existing_by_name = {item.field_name: item for item in existing}

        def save_corrected_field(field_name, value, force_manual=False):
            item = existing_by_name.get(field_name)
            if item is None and not value:
                return
            if item is None:
                ExtractedField.objects.create(
                    scan=scan, field_name=field_name, value=value, confidence=100,
                    source_type=ExtractedField.SourceType.MANUAL,
                )
                return
            changed = value != item.value or force_manual
            if changed:
                if not item.original_value:
                    item.original_value = item.value
                    item.original_confidence = item.confidence
                    if item.source_image_id:
                        source_text = item.source_image.extracted_text
                    else:
                        source_text = scan.extracted_text
                    item.original_evidence = next((line.strip() for line in source_text.splitlines()
                                                   if item.value and item.value.casefold() in line.casefold()), "No matching OCR line was found.")
                item.value = value
                item.confidence = 100
                item.source_type = ExtractedField.SourceType.MANUAL
                item.review_reason = ""
            item.save()
        if action == "recheck_fields" and existing:
            for extracted in existing:
                value = request.POST.get(f"field_{extracted.field_name}", extracted.value).strip()
                save_corrected_field(extracted.field_name, value)
            existing_names = {item.field_name for item in existing}
            for field_name, _label in manual_fields:
                if field_name in existing_names:
                    continue
                value = request.POST.get(f"field_{field_name}", "").strip()
                save_corrected_field(field_name, value, force_manual=True)
        else:
            for field_name, _label in manual_fields:
                value = request.POST.get(field_name, "").strip()
                save_corrected_field(field_name, value, force_manual=action == "manual_verify")
        if not existing and action == "recheck_fields":
            for field_name, _label in manual_fields:
                value = request.POST.get(f"field_{field_name}", request.POST.get(field_name, "")).strip()
                save_corrected_field(field_name, value)
        scan.manually_verified = True
        scan.save(update_fields=("manually_verified",))
        from .services.rules import evaluate
        evaluate(scan)
        try:
            scan.report
        except Report.DoesNotExist:
            pass
        else:
            generate_pdf(scan)
        messages.success(request, "Your corrected details were saved and the checks were reassessed.")
        return redirect("scanner:result", scan_id=scan.pk)
    existing_fields = {item.field_name: item.value for item in scan.extracted_fields.all()}
    manual_form_fields = [
        {"name": name, "label": field_label(name), "value": existing_fields.get(name, "")}
        for name, label in manual_fields
    ]
    rule_results = list(scan.rule_results.select_related("rule", "source_image"))
    assessment = assessment_from_results(rule_results)
    checked_rule_count = assessment["assessed"]
    applicable_rule_count = assessment["applicable"]
    needs_review_count = assessment["needs_review"]
    threshold = getattr(settings, "OCR_MIN_CONFIDENCE", 40)
    global_ocr_warning = scan.ocr_confidence is not None and scan.ocr_confidence < threshold
    extracted_fields = list(scan.extracted_fields.all())
    extracted_names = {field.field_name for field in extracted_fields}
    missing_manual_fields = [
        {"name": name, "label": field_label(name)}
        for name, label in manual_fields if name not in extracted_names
    ]
    for field in extracted_fields:
        field.is_uncertain = field.source_type != ExtractedField.SourceType.MANUAL and (
            bool(field.review_reason) or field.confidence is None or field.confidence < 50
        )
    extracted_by_name = {field.field_name: field for field in extracted_fields}
    for date_field in ("mfg_date", "expiry_date"):
        if date_field not in extracted_by_name:
            extracted_fields.append(SimpleNamespace(
                field_name=date_field, value="", confidence=None, source_type="ocr",
                source_image=None, source_pass="", original_value="", original_confidence=None,
                original_evidence="", review_reason="", alternatives=[], is_uncertain=True,
            ))
    ocr_images = list(scan.images.all())
    side_totals = {}
    for image in ocr_images:
        side_totals[image.side] = side_totals.get(image.side, 0) + 1
    side_seen = {}
    for index, image in enumerate(ocr_images, start=1):
        side_seen[image.side] = side_seen.get(image.side, 0) + 1
        base_label = image.get_side_display()
        image.display_label = f"{base_label} {side_seen[image.side]}" if side_totals[image.side] > 1 else base_label
        source_text = image.extracted_text or (scan.extracted_text if len(ocr_images) == 1 else "")
        image.display_ocr_text = display_ocr_text(source_text)
        image.ocr_tab_id = f"ocr-image-{image.pk or index}"
    field_names = {
        "Product Details": {"brand", "generic_name", "barcode", "batch_number", "fssai_license", "country_of_origin"},
        "Manufacturer Details": {"manufacturer", "manufacturer_address", "marketer", "packed_by", "imported_by", "manufacturer_pin", "marketer_pin", "packed_by_pin", "imported_by_pin"},
        "Pricing": {"mrp", "mrp_currency", "mrp_inclusive_of_taxes", "unit_sale_price"},
        "Quantity": {"net_quantity", "net_quantity_conflict", "net_quantities", "net_quantity_front", "net_quantity_back"},
        "Dates": {"mfg_date", "pkd_date", "packed_on", "expiry_date"},
        "Consumer Care": {"consumer_care", "phone", "email"},
    }
    grouped_fields = {group: [] for group in field_names}
    for field in extracted_fields:
        group = next((group for group, names in field_names.items() if field.field_name in names), "Product Details")
        grouped_fields[group].append(field)
    field_groups = [
        {"name": group, "fields": items}
        for group, items in grouped_fields.items() if items
    ]
    result_groups = {
        "passed_results": [item for item in rule_results if item.status == RuleResult.Status.PASS],
        "failed_results": [item for item in rule_results if item.status == RuleResult.Status.FAIL],
        "needs_review_results": [item for item in rule_results if item.status == RuleResult.Status.NEEDS_REVIEW],
        "na_results": [item for item in rule_results if item.status == RuleResult.Status.NOT_APPLICABLE],
    }
    filter_params = request.GET.copy()
    filter_params.pop("page", None)
    return render(request, "scanner/result.html", {
        "scan": scan,
        "assessment": assessment,
        "assessment_verdict_label": dict(Scan.Status.choices).get(assessment["verdict"], "Needs review"),
        "assessment_score_display": assessment["display"],
        "manual_form_fields": manual_form_fields,
        "violations": scan.violations(),
        "violation_ids": set(scan.violations().values_list("pk", flat=True)),
        "scan_summary": scan.scan_summary,
        "checked_rule_count": checked_rule_count,
        "applicable_rule_count": applicable_rule_count,
        "needs_review_count": needs_review_count,
        "has_review_items": bool(needs_review_count),
        "global_ocr_warning": global_ocr_warning,
        "can_edit_product": not scan.product or scan.product.name == "Unidentified product",
        "extracted_fields": extracted_fields,
        "ocr_images": ocr_images,
        "field_groups": field_groups,
        "missing_manual_fields": missing_manual_fields,
        "ocr_percent": round(scan.ocr_confidence or 0),
        "no_text_found": not bool(scan.extracted_text.strip()),
        "legacy_ocr_display_text": display_ocr_text(scan.extracted_text),
        "back_image_missing": not scan.images.filter(side=ScanImage.Side.BACK).exists(),
        "filter_query": filter_params.urlencode(),
        **result_groups,
    })


@login_required
def download_report(request, scan_id):
    scan = get_object_or_404(
        Scan.objects.select_related("product", "user").prefetch_related(
            "images", "extracted_fields", "rule_results__rule"
        ),
        pk=scan_id,
        user=request.user,
    )
    generate_pdf(scan)
    report = scan.report
    return FileResponse(report.pdf_file.open("rb"), as_attachment=True, filename=f"scan-{scan.pk}-report.pdf")


@login_required
def rescan(request, scan_id):
    if request.method != "POST":
        return redirect("scanner:result", scan_id=scan_id)
    scan = get_object_or_404(Scan, pk=scan_id, user=request.user)
    try:
        process_scan(scan)
    except Exception:
        logger.exception("Scan reprocessing failed for scan %s", scan.pk)
        _mark_scan_processing_error(scan)
        messages.warning(request, "The scan could not be completed. Try clearer photos or review the details manually.")
    try:
        scan.report
    except Report.DoesNotExist:
        pass
    else:
        generate_pdf(scan)
    messages.success(request, "The uploaded images have been scanned again.")
    return redirect("scanner:result", scan_id=scan.pk)


@login_required
def history(request):
    scans = request.user.scans.select_related("product").prefetch_related("images", "rule_results")
    selected_status = request.GET.get("status", "")
    search_query = request.GET.get("q", "").strip()
    valid_statuses = {value for value, _ in Scan.Status.choices}
    if selected_status not in valid_statuses:
        selected_status = ""
    if search_query:
        scans = scans.filter(Q(product__name__icontains=search_query) | Q(product__brand__icontains=search_query))
    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()
    score_min = request.GET.get("score_min", "").strip()
    score_max = request.GET.get("score_max", "").strip()
    if date_from and parse_date(date_from):
        scans = scans.filter(created_at__date__gte=parse_date(date_from))
    elif date_from:
        date_from = ""
    if date_to and parse_date(date_to):
        scans = scans.filter(created_at__date__lte=parse_date(date_to))
    elif date_to:
        date_to = ""
    score_min_value = score_max_value = None
    try:
        if score_min:
            score_min_value = float(score_min)
            if not 0 <= score_min_value <= 100:
                raise ValueError
    except ValueError:
        score_min, score_min_value = "", None
    try:
        if score_max:
            score_max_value = float(score_max)
            if not 0 <= score_max_value <= 100:
                raise ValueError
    except ValueError:
        score_max, score_max_value = "", None
    sort_options = {
        "newest": "-created_at", "oldest": "created_at",
        "score_high": "-score", "score_low": "score",
        "name": "product__name", "name_desc": "-product__name",
    }
    selected_sort = request.GET.get("sort", "newest")
    if selected_sort not in sort_options:
        selected_sort = "newest"
    scan_list = list(scans)
    for listed_scan in scan_list:
        listed_scan.assessment = assessment_for_scan(listed_scan)
        listed_scan.score_display = listed_scan.assessment["display"]
        listed_scan.assessment_verdict_label = dict(Scan.Status.choices).get(listed_scan.assessment["verdict"], "Needs review")
    if selected_status:
        scan_list = [item for item in scan_list if item.assessment["verdict"] == selected_status]
    if score_min_value is not None:
        scan_list = [item for item in scan_list if item.assessment["score"] is not None and item.assessment["score"] >= score_min_value]
    if score_max_value is not None:
        scan_list = [item for item in scan_list if item.assessment["score"] is not None and item.assessment["score"] <= score_max_value]
    if selected_sort == "score_high":
        scan_list.sort(key=lambda item: (item.assessment["score"] is not None, item.assessment["score"] or -1, item.pk), reverse=True)
    elif selected_sort == "score_low":
        scan_list.sort(key=lambda item: (item.assessment["score"] is None, item.assessment["score"] or 0, -item.pk))
    else:
        scan_list.sort(key=lambda item: (
            item.product.name.casefold() if item.product_id else "",
            item.pk,
        ), reverse=selected_sort == "name_desc") if selected_sort in {"name", "name_desc"} else scan_list.sort(
            key=lambda item: (item.created_at, item.pk), reverse=selected_sort == "newest"
        )
    filter_params = request.GET.copy()
    filter_params.pop("page", None)
    page_obj = Paginator(scan_list, 10).get_page(request.GET.get("page"))
    return render(request, "scanner/history.html", {
        "page_obj": page_obj,
        "scans": page_obj.object_list,
        "status_choices": Scan.Status.choices,
        "selected_status": selected_status,
        "search_query": search_query,
        "date_from": date_from, "date_to": date_to,
        "score_min": score_min, "score_max": score_max,
        "selected_sort": selected_sort,
        "filter_query": filter_params.urlencode(),
    })


@login_required
def delete_scan(request, scan_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    scan = get_object_or_404(Scan, pk=scan_id, user=request.user)
    scan.delete()
    messages.success(request, "The scan was deleted.")
    return redirect("scanner:history")
