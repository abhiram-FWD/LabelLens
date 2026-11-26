"""Run active database rules against fields extracted from one scan."""

import re
from datetime import date, datetime

from django.conf import settings
from django.db import transaction

from scanner.models import ExtractedField, Rule, RuleResult, Scan
from scanner.services.extractor import extract_fields
from scanner.services.assessment import assessment_from_results


def _value(fields, key):
    raw = fields.get(key)
    if isinstance(raw, dict):
        raw = raw.get("value", "")
    return str(raw or "").strip()


def _missing(value, label):
    if not value:
        return RuleResult.Status.FAIL, "", f"No {label} was detected in the label text."
    return None


def _valid_label_date(value):
    month_year = re.fullmatch(r"(\d{1,2})\s*[/.-]\s*(\d{2}|\d{4})", value)
    if month_year:
        return 1 <= int(month_year.group(1)) <= 12
    month_name_year = re.fullmatch(r"([A-Za-z]{3,9})\s+(\d{4})", value)
    if month_name_year:
        try:
            datetime.strptime(month_name_year.group(1)[:3], "%b")
            return True
        except ValueError:
            return False
    numeric = re.fullmatch(r"(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{2,4})", value)
    named = re.fullmatch(r"(\d{1,2})\s*[/.-]\s*([A-Za-z]{3,9})\s*[/.-]\s*(\d{2,4})", value)
    try:
        if numeric:
            month = int(numeric.group(2))
            year = int(numeric.group(3))
            day = int(numeric.group(1))
        elif named:
            month = datetime.strptime(named.group(2)[:3], "%b").month
            year = int(named.group(3))
            day = int(named.group(1))
        else:
            return False
        date(year + (2000 if year < 100 else 0), month, day)
        return True
    except ValueError:
        return False


def _date_evidence_matches_role(field_name, evidence):
    """Require OCR evidence to identify the declaration type for a date."""
    if field_name == "mfg_date":
        return bool(re.search(
            r"\b(?:mfg|mfd|pkd)\b|\bmanufacturing\s+date\b|"
            r"\bmanufactured(?:\s+(?:on|date))?\b(?!\s+by)|"
            r"\bpacked(?:\s+(?:on|date))?\b(?!\s+by)|\bpacking\s+date\b",
            evidence or "", re.I,
        ))
    if field_name == "expiry_date":
        return bool(re.search(
            r"\b(?:exp(?:iry)?|use\s*(?:by|before)|best\s*before)\b",
            evidence or "", re.I,
        ))
    return True


def _field_evidence_matches_declaration(field_name, evidence):
    patterns = {
        "net_quantity": r"\bnet\s*(?:weight|wt\.?|qty\.?|quantity|contents?|content)\b",
        "mrp": r"\b(?:m\s*\.?\s*r\s*\.?\s*p\.?|maximum\s*retail\s*price)\b",
    }
    pattern = patterns.get(field_name)
    return bool(pattern and re.search(pattern, evidence or "", re.I))


def validate_manufacturer(fields):
    parties = [_value(fields, name) for name in ("manufacturer", "manufacturer_address", "packed_by", "imported_by")]
    parties = [value for value in parties if value]
    if not parties:
        return RuleResult.Status.FAIL, "", "No manufacturer, packer, or importer declaration was detected."
    complete = [value for value in parties if re.search(r"[A-Za-z]{2,}", value)
                and re.search(r"(?<!\d)[1-9]\d{2}\s?\d{3}(?!\d)", value)]
    if complete:
        return RuleResult.Status.PASS, "; ".join(complete), "A responsible party name and postal address with a valid PIN code were detected."
    return RuleResult.Status.NEEDS_REVIEW, "; ".join(parties), "Manufacturer details were partially detected, but the complete postal address could not be verified."


def validate_generic_name(fields):
    value = _value(fields, "generic_name")
    missing = _missing(value, "common or generic product name")
    if missing:
        return missing
    if value.casefold() in {"n/a", "na", "unknown", "none", "product", "item"}:
        return RuleResult.Status.FAIL, value, "The detected text is only a placeholder, not a usable generic product name."
    return RuleResult.Status.PASS, value, "A common or generic product name was detected."


def validate_net_quantity(fields):
    value = _value(fields, "net_quantity")
    missing = _missing(value, "net quantity")
    if missing:
        return missing
    conflict = _value(fields, "net_quantity_conflict")
    if conflict:
        return RuleResult.Status.NEEDS_REVIEW, value, f"Multiple quantity values were detected and require verification: {conflict}."
    if not re.fullmatch(r"\d+(?:[.,]\d{1,3})?\s*(?:kg|g|mg|ml|l|cl|units?|pcs?\.?|pieces?|count|ct)", value, re.I):
        return RuleResult.Status.FAIL, value, "The quantity was detected, but its unit or count declaration could not be verified."
    return RuleResult.Status.PASS, value, "Net quantity uses a recognized metric unit."


def validate_mfg_date(fields):
    value = _value(fields, "mfg_date")
    missing = _missing(value, "month and year of manufacture or packing")
    if missing:
        return missing
    if not _valid_label_date(value):
        return RuleResult.Status.FAIL, value, "The manufacture or packing date is not in a recognizable month/year format."
    return RuleResult.Status.PASS, value, "A manufacture or packing month and year was detected."


def validate_mrp(fields):
    value = _value(fields, "mrp")
    missing = _missing(value, "maximum retail price")
    if missing:
        return missing
    amount = value.replace(",", "")
    if not re.fullmatch(r"(?:0|[1-9]\d{0,6})(?:\.\d{1,2})?", amount) or float(amount) <= 0:
        return RuleResult.Status.FAIL, value, "The MRP amount is not a valid positive price."
    source = _value(fields, "_source_text")
    if (
        _value(fields, "mrp_inclusive_of_taxes").casefold() not in {"yes", "true", "1"}
        and not re.search(r"incl(?:usive)?\.?\s*(?:of\s*)?all\s+tax(?:es|s)?", source, re.I)
    ):
        return RuleResult.Status.FAIL, value, "MRP was detected, but the label text does not say it is inclusive of all taxes."
    return RuleResult.Status.PASS, value, "MRP is stated as inclusive of all taxes."


def validate_consumer_care(fields):
    value = " ".join(filter(None, (
        _value(fields, "phone"), _value(fields, "email"), _value(fields, "consumer_care"),
    )))
    missing = _missing(value, "consumer-care contact details")
    if missing:
        return missing
    if not re.search(r"(?:\+?91[\s-]?)?[6-9](?:[\s-]?\d){9}|\b0\d{2,4}[\s-]?\d{6,8}\b|\b1800[\s-]?\d{3}[\s-]?\d{4}\b|[\w.+-]+@[\w.-]+\.[A-Z]{2,}", value, re.I):
        return RuleResult.Status.FAIL, value, "Consumer-care information was mentioned, but no recognizable phone number or email was detected."
    return RuleResult.Status.PASS, value, "Consumer-care contact information includes a phone number or email."


def validate_country_of_origin(fields):
    value = _value(fields, "country_of_origin")
    if not value:
        return RuleResult.Status.FAIL, "", "An ‘Imported by’ declaration was detected, but no country of origin was found."
    if not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,60}", value) or value.casefold() in {"unknown", "n/a", "na"}:
        return RuleResult.Status.FAIL, value, "A country-of-origin declaration was detected, but its value could not be recognized as a country name."
    return RuleResult.Status.PASS, value, "Country of origin was detected for an imported product."


def validate_expiry_date(fields):
    value = _value(fields, "expiry_date")
    missing = _missing(value, "expiry or best-before date")
    if missing:
        return missing
    if not _valid_label_date(value):
        return RuleResult.Status.FAIL, value, "The expiry declaration could not be read as a recognizable date."
    return RuleResult.Status.PASS, value, "An expiry or best-before date was detected."


_FOREIGN_COUNTRIES = (
    "United States", "USA", "United Kingdom", "UK", "China", "Japan", "South Korea",
    "North Korea", "Thailand", "Vietnam", "Singapore", "Malaysia", "Indonesia",
    "Bangladesh", "Pakistan", "Nepal", "Sri Lanka", "Australia", "Canada", "Germany",
    "France", "Italy", "Spain", "Netherlands", "Switzerland", "Turkey", "UAE",
    "United Arab Emirates", "Saudi Arabia", "Brazil", "Mexico", "New Zealand",
)


def applies_country_of_origin(fields):
    """Return True/False when applicability is known, or None when OCR is weak."""
    source = _value(fields, "_source_text")
    if re.search(r"\bimported\s+by\b", source, re.I):
        return True
    foreign = r"\b(?:" + "|".join(re.escape(country) for country in _FOREIGN_COUNTRIES) + r")\b"
    if re.search(foreign, source, re.I):
        return True
    if fields.get("_ocr_unreliable"):
        return None
    return False


VALIDATORS = {
    "manufacturer_address": validate_manufacturer,
    "generic_name": validate_generic_name,
    "net_quantity": validate_net_quantity,
    "mfg_date": validate_mfg_date,
    "mrp": validate_mrp,
    "consumer_care": validate_consumer_care,
    "country_of_origin": validate_country_of_origin,
    "expiry_date": validate_expiry_date,
}

APPLICABILITY = {code: (lambda _fields: True) for code in VALIDATORS}
APPLICABILITY["country_of_origin"] = applies_country_of_origin
APPLICABILITY["expiry_date"] = lambda fields: bool(
    _value(fields, "expiry_date")
    or re.search(r"\b(?:use\s*(?:by|before)|best\s*before|exp(?:iry)?\.?)\b", _value(fields, "_source_text"), re.I)
)

_DECLARATION_KEYWORDS = {
    "manufacturer_address": r"\b(?:manufactured|manufacturer|mfd\.?|packed|pkd\.?|imported)\s+by\b",
    "net_quantity": r"\bnet\s*(?:weight|wt|qty|quantity|content|contents)\b",
    "mfg_date": r"\b(?:pkd|mfg|mfd|packing\s+date|manufacturing\s+date|packed(?:\s+(?:on|date))?|manufactured(?:\s+(?:on|date))?)\b(?!\s+by)",
    "mrp": r"\b(?:m\s*\.?\s*r\s*\.?\s*p\.?|maximum\s+retail\s+price)\b",
    "consumer_care": r"\b(?:consumer\s*care|customer\s*care|consumer\s+complaints?|toll\s*free|tel\.?|email)\b",
    "country_of_origin": r"\b(?:country\s+of\s+origin|made\s+in|product\s+of)\b",
    "expiry_date": r"\b(?:use\s*(?:by|before)|best\s*before|exp(?:iry)?\.?)\b",
}


def _has_declaration(code, fields):
    pattern = _DECLARATION_KEYWORDS.get(code)
    if code == "manufacturer_address":
        pattern = r"\b(?:manufactured|manufacturer|mfd\.?|packed|pkd\.?|imported)\s+by\b"
    return bool(pattern and re.search(pattern, _value(fields, "_source_text"), re.I))


def _has_readable_back_ocr(scan, low_confidence):
    """Require meaningful back-label OCR before calling an unseen declaration absent."""
    if low_confidence or scan.manually_verified:
        return bool(scan.manually_verified)
    for image in scan.images.filter(side="back"):
        text = image.extracted_text or ""
        confidence = image.ocr_confidence
        if (confidence is not None and confidence >= 50
                and len(text.strip()) >= 120
                and len(re.findall(r"[A-Za-z0-9]+", text)) >= 15):
            return True
    return False


def _category_applies(rule, scan):
    categories = [item.strip().casefold() for item in rule.applicable_categories.split(",") if item.strip()]
    if not categories:
        return True
    category = (scan.product.category if scan.product_id else "").strip().casefold()
    return bool(category and any(category == item or category.startswith(item + " ") for item in categories))


def _detected_value(code, fields):
    if code == "manufacturer_address":
        return _value(fields, "manufacturer") or _value(fields, "manufacturer_address") or _value(fields, "packed_by") or _value(fields, "imported_by")
    if code == "consumer_care":
        return " ".join(filter(None, (
            _value(fields, "phone"), _value(fields, "email"), _value(fields, "consumer_care"),
        )))
    return _value(fields, code)


def _evidence_for_rule(scan, rule, fields, found_value, status):
    field_names = {
        "manufacturer_address": ("manufacturer", "manufacturer_address", "packed_by", "imported_by"),
        "consumer_care": ("phone", "email", "consumer_care"),
    }
    names = field_names.get(rule.code, (rule.code,))
    field_candidates = [fields[name] for name in names
                        if isinstance(fields.get(name), dict) and fields[name].get("value")]
    matching_fields = [candidate for candidate in field_candidates
                       if found_value and str(candidate.get("value", "")).casefold() in found_value.casefold()]
    field = next((candidate for candidate in matching_fields
                  if candidate.get("source_type") == ExtractedField.SourceType.MANUAL), None)
    if field is None:
        field = (matching_fields or field_candidates or [None])[0]
    if field and field.get("source_type") == ExtractedField.SourceType.MANUAL:
        return {
            "source_image_id": field.get("source_image_id"), "evidence": "Entered manually",
            "matched_line": "", "preceding_line": "", "following_line": "",
            "ocr_confidence": field.get("original_confidence"), "source_type": RuleResult.SourceType.MANUAL,
            "source_pass": field.get("source_pass", ""),
            "original_ocr_value": field.get("original_value", ""),
            "original_ocr_evidence": field.get("original_evidence", ""),
        }
    if field and field.get("evidence"):
        evidence_lines = [line for line in field["evidence"].splitlines() if line.strip()]
        source_image_id = field.get("source_image_id")
        if source_image_id is None and evidence_lines:
            source_text = "\n".join(evidence_lines).casefold()
            matching_image = next((
                image for image in scan.images.all()
                if source_text in (image.extracted_text or "").casefold()
            ), None)
            if matching_image:
                source_image_id = matching_image.pk
        return {
            "source_image_id": source_image_id, "evidence": field["evidence"],
            "matched_line": evidence_lines[0] if evidence_lines else "",
            "preceding_line": "", "following_line": "\n".join(evidence_lines[1:]),
            "ocr_confidence": field.get("confidence"), "source_type": RuleResult.SourceType.OCR,
            "source_pass": field.get("source_pass", ""),
            "original_ocr_value": field.get("value", found_value),
            "original_ocr_evidence": field["evidence"],
        }
    keyword = _DECLARATION_KEYWORDS.get(rule.code)
    images = list(scan.images.all())
    source_text = _value(fields, "_source_text")
    ranked = []
    for image in images:
        text = image.extracted_text or (source_text if not any(item.extracted_text for item in images) else "")
        value_match = bool(found_value and found_value.casefold() in text.casefold())
        keyword_match = bool(keyword and re.search(keyword, text, re.I))
        ranked.append((int(value_match) * 2 + int(keyword_match), image, text))
    ranked.sort(key=lambda item: (item[0], item[1].side == "back"), reverse=True)
    source_image = None
    if field and field.get("source_image_id"):
        source_image = next((image for image in images if image.pk == field["source_image_id"]), None)
    if source_image is None and ranked:
        source_image = ranked[0][1]
    text = source_image.extracted_text if source_image and source_image.extracted_text else source_text
    if not text:
        return {
            "source_image_id": source_image.pk if source_image else None,
            "evidence": "OCR did not provide a matching text snippet.", "matched_line": "",
            "preceding_line": "", "following_line": "", "ocr_confidence": scan.ocr_confidence,
            "source_type": RuleResult.SourceType.OCR, "original_ocr_value": "", "original_ocr_evidence": "",
            "source_pass": field.get("source_pass", "") if field else "",
        }

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    matched = next((index for index, line in enumerate(lines) if found_value and found_value.casefold() in line.casefold()), None)
    if matched is None and keyword:
        matched = next((index for index, line in enumerate(lines) if re.search(keyword, line, re.I)), None)
    if matched is None:
        return {
            "source_image_id": source_image.pk if source_image else None,
            "evidence": "No matching OCR line was found.", "matched_line": "",
            "preceding_line": "", "following_line": "", "ocr_confidence": scan.ocr_confidence,
            "source_type": RuleResult.SourceType.OCR, "original_ocr_value": "",
            "original_ocr_evidence": "",
            "source_pass": field.get("source_pass", "") if field else "",
        }
    matched_line = lines[matched] if matched is not None else ""
    preceding = lines[matched - 1] if matched is not None and matched > 0 else ""
    following = lines[matched + 1] if matched is not None and matched + 1 < len(lines) else ""
    snippet = " ".join(line for line in (preceding, matched_line, following) if line)
    return {
        "source_image_id": source_image.pk if source_image else None,
        "evidence": snippet[:1200] or "OCR did not provide a matching text snippet.",
        "matched_line": matched_line, "preceding_line": preceding, "following_line": following,
        "ocr_confidence": (field.get("original_confidence") if field and field.get("original_confidence") is not None else (field.get("confidence") if field else scan.ocr_confidence)),
        "source_type": RuleResult.SourceType.OCR, "original_ocr_value": found_value,
        "original_ocr_evidence": snippet[:1200],
        "source_pass": field.get("source_pass", "") if field else "",
    }


@transaction.atomic
def evaluate(scan):
    """Reassess all active rules and persist one shared score and verdict."""
    if not isinstance(scan, Scan):
        scan = Scan.objects.get(pk=scan)

    fields = extract_fields(scan.extracted_text or "")
    extracted = list(ExtractedField.objects.filter(scan=scan).select_related("source_image"))
    field_records = {item.field_name: item for item in extracted}
    fields.update({item.field_name: {
        "value": item.value,
        "confidence": item.confidence,
        "source_image_id": item.source_image_id,
        "source_type": item.source_type,
        "evidence": item.original_evidence,
        "evidence_coordinates": item.original_evidence_coordinates,
        "source_pass": item.source_pass,
        "original_value": item.original_value,
        "original_confidence": item.original_confidence,
        "original_evidence": item.original_evidence,
        "source_pass": item.source_pass,
        "alternatives": item.alternatives,
        "review_reason": item.review_reason,
    } for item in extracted})
    fields["_source_text"] = {"value": scan.extracted_text or ""}

    rules = list(Rule.objects.filter(is_active=True).order_by("code"))
    active_ids = [rule.pk for rule in rules]
    if active_ids:
        scan.rule_results.exclude(rule_id__in=active_ids).delete()
    else:
        scan.rule_results.all().delete()

    min_confidence = getattr(settings, "OCR_MIN_CONFIDENCE", 40)
    low_confidence = not scan.manually_verified and (
        scan.ocr_confidence is None or scan.ocr_confidence < min_confidence
    )
    readable_back = _has_readable_back_ocr(scan, low_confidence)
    ocr_unreliable = low_confidence or (not readable_back and not scan.manually_verified)
    fields["_ocr_unreliable"] = ocr_unreliable

    for rule in rules:
        applicability = APPLICABILITY.get(rule.code, lambda _fields: True)(fields)
        if rule.code == "expiry_date" and rule.applicable_categories:
            applicability = _category_applies(rule, scan)
        elif rule.code == "expiry_date" and ocr_unreliable:
            applicability = None
        if applicability and not _category_applies(rule, scan):
            applicability = False
        if applicability is False:
            status = RuleResult.Status.NOT_APPLICABLE
            found_value = _detected_value(rule.code, fields)
            message = "This check does not apply to the declarations detected on this label."
        elif applicability is None:
            status = RuleResult.Status.NEEDS_REVIEW
            found_value = _detected_value(rule.code, fields)
            message = "Applicability could not be determined reliably from the label text."
        else:
            validator = VALIDATORS.get(rule.code)
            if validator:
                status, found_value, message = validator(fields)
                field_value = field_records.get(rule.code)
                if rule.code == "manufacturer_address":
                    field_value = next((field_records[name] for name in ("manufacturer", "manufacturer_address", "packed_by", "imported_by") if name in field_records), None)
                if rule.code == "consumer_care":
                    field_value = next((field_records[name] for name in ("phone", "email", "consumer_care") if name in field_records), None)
                manual_value = bool(field_value and field_value.source_type == ExtractedField.SourceType.MANUAL)
                field_confidence = field_value.confidence if field_value else None
                wrong_date_heading = (
                    rule.code in {"mfg_date", "expiry_date"}
                    and field_value and not manual_value
                    and status == RuleResult.Status.PASS
                    and not _date_evidence_matches_role(rule.code, field_value.original_evidence)
                )
                missing_declaration_evidence = (
                    rule.code in {"net_quantity", "mrp"}
                    and field_value and not manual_value
                    and status == RuleResult.Status.PASS
                    and not _field_evidence_matches_declaration(rule.code, field_value.original_evidence)
                )
                if wrong_date_heading:
                    status = RuleResult.Status.NEEDS_REVIEW
                    message = f"{rule.name} date evidence does not confirm the matching declaration heading. Verify the association."
                elif missing_declaration_evidence:
                    status = RuleResult.Status.NEEDS_REVIEW
                    message = f"{rule.name} value was extracted, but its declaration heading is not present in the field evidence. Verify the value."
                elif field_value and field_value.review_reason and not manual_value:
                    status = RuleResult.Status.NEEDS_REVIEW
                    message = field_value.review_reason
                elif status == RuleResult.Status.PASS and not manual_value and (
                    (field_confidence is not None and field_confidence < 50)
                    or (field_confidence is None and low_confidence)
                ):
                    status = RuleResult.Status.NEEDS_REVIEW
                    message = f"{rule.name} was detected, but its OCR confidence is below 50% and needs verification."
                elif status == RuleResult.Status.NEEDS_REVIEW:
                    pass
                elif status == RuleResult.Status.FAIL:
                    declaration_found = _has_declaration(rule.code, fields)
                    if not found_value and declaration_found:
                        status = RuleResult.Status.NEEDS_REVIEW
                        message = f"{rule.name} declaration was detected, but its value could not be extracted reliably."
                    elif not found_value and not readable_back:
                        status = RuleResult.Status.NEEDS_REVIEW
                        message = f"{rule.name} could not be assessed reliably from the available label images."
                    elif found_value and not manual_value and field_confidence is not None and field_confidence < 50:
                        status = RuleResult.Status.NEEDS_REVIEW
                        message = f"{rule.name} was detected, but its OCR confidence is below 50% and needs verification."
                    elif found_value and not manual_value and field_confidence is None:
                        status = RuleResult.Status.NEEDS_REVIEW
                        message = f"{rule.name} could not be validated reliably from the OCR text."
                    elif not found_value:
                        message = f"No {rule.name.lower()} declaration was detected on the readable back label."
            else:
                status = RuleResult.Status.NEEDS_REVIEW
                found_value = ""
                message = f"No validator is registered for rule code '{rule.code}'."

        evidence_data = _evidence_for_rule(scan, rule, fields, found_value, status)
        suggested_fix = rule.fix_suggestion if status in (RuleResult.Status.FAIL, RuleResult.Status.NEEDS_REVIEW) else ""

        RuleResult.objects.update_or_create(
            scan=scan,
            rule=rule,
            defaults={
                "status": status,
                "found_value": found_value,
                "source_image_id": evidence_data["source_image_id"],
                "evidence": evidence_data["evidence"],
                "matched_line": evidence_data["matched_line"],
                "preceding_line": evidence_data["preceding_line"],
                "following_line": evidence_data["following_line"],
                "ocr_confidence": evidence_data["ocr_confidence"],
                "source_type": evidence_data["source_type"],
                "source_pass": evidence_data.get("source_pass", ""),
                "original_ocr_value": evidence_data["original_ocr_value"],
                "original_ocr_evidence": evidence_data["original_ocr_evidence"],
                "legal_reference": rule.legal_reference,
                "suggested_fix": suggested_fix,
                "message": message,
            },
        )
    if hasattr(scan, "_prefetched_objects_cache"):
        scan._prefetched_objects_cache.pop("rule_results", None)
    assessment = assessment_from_results(scan.rule_results.all())
    scan.score = assessment["score"]
    scan.status = assessment["verdict"]
    scan.save(update_fields=("score", "status"))
    return scan.score
