"""Shared OCR, extraction, barcode, and rule-processing pipeline."""

from collections import defaultdict
import logging
import re
from time import perf_counter
from rapidfuzz import fuzz

from scanner.models import ExtractedField, Product
from scanner.services.barcode import decode_barcodes
from scanner.services.extractor import extract_fields
from scanner.services.ocr import run_ocr
from scanner.services.rules import evaluate

logger = logging.getLogger(__name__)


def _value_key(value):
    return " ".join(str(value or "").casefold().split())


def _merge_fields(candidates):
    """Choose each field from its field-level evidence across OCR passes."""
    grouped = defaultdict(list)
    date_pairs = []
    for candidate in candidates:
        fields, attempt_info = candidate[:2]
        if isinstance(attempt_info, dict):
            attempt_confidence = float(attempt_info.get("confidence") or 0)
            source_pass = attempt_info.get("source_pass") or f"{attempt_info.get('variant', 'ocr')}_psm{attempt_info.get('psm', '')}"
        else:
            attempt_confidence = float(attempt_info or 0)
            source_pass = ""
        source_image_id = candidate[2].pk if len(candidate) > 2 else None
        source_side = getattr(candidate[2], "side", "") if len(candidate) > 2 else ""
        region = attempt_info.get("region", "") if isinstance(attempt_info, dict) else ""
        if all(fields.get(name) and fields[name].get("value") for name in ("mfg_date", "expiry_date")):
            date_pairs.append({
                "mfg_date": dict(fields["mfg_date"]),
                "expiry_date": dict(fields["expiry_date"]),
                "source_image_id": source_image_id,
                "source_pass": source_pass,
                "attempt_confidence": attempt_confidence,
                "source_side": source_side, "region": region,
            })
        for name, result in fields.items():
            if not result:
                continue
            field_confidence = float(result.get("confidence") or 0)
            semantic_score = 0
            if name == "email":
                local = result["value"].split("@", 1)[0].replace("_", ".").replace("-", ".")
                semantic_score = fuzz.ratio(local.casefold(), "consumer.care") * 0.08
            grouped[name].append({
                "value": result["value"], "confidence": field_confidence,
                "score": field_confidence + semantic_score,
                "source_image_id": source_image_id, "source_pass": source_pass,
                "attempt_confidence": attempt_confidence,
                "source_side": source_side, "region": region,
                "evidence": result.get("evidence", ""),
                "evidence_coordinates": list(result.get("evidence_coordinates") or []),
                "review_reason": result.get("review_reason", ""),
                "alternatives": list(result.get("alternatives") or []),
                "brand_corroborated": bool(
                    name == "brand" and any(
                        re.search(rf"\b{re.escape(result['value'])}\b", fields.get(party, {}).get("value", ""), re.I)
                        for party in ("manufacturer", "marketer", "packed_by", "imported_by")
                        if isinstance(fields.get(party), dict)
                    )
                ),
            })

    merged = {}
    for name, values in grouped.items():
        by_value = defaultdict(list)
        for item in values:
            by_value[_value_key(item["value"])].append(item)
        best_group = max(by_value.values(), key=lambda group: max(item["score"] for item in group) + min(6, 2 * (len(group) - 1)))
        def candidate_score(item):
            score = item["score"]
            if name in {"manufacturer", "marketer", "packed_by", "imported_by"}:
                value = item["value"]
                role_heading = {
                    "manufacturer": r"manufactured\s+by|manufacturer",
                    "marketer": r"marketed\s+by",
                    "packed_by": r"packed\s+by|pkd\.?\s+by",
                    "imported_by": r"imported\s+by",
                }[name]
                if re.search(role_heading, item.get("evidence", ""), re.I):
                    score += 24
                has_pin = bool(re.search(r"(?<!\d)[1-9]\d{2}\s?\d{3}(?!\d)", value))
                has_street = bool(re.search(r"\b(?:plot|road|street|sector|industrial|estate|lane|avenue|tower|floor|building|nagar)\b", value, re.I))
                has_locality = bool(re.search(r",\s*[A-Za-z][A-Za-z .'-]{2,}", value))
                # Completeness is a stronger signal than a small OCR
                # confidence difference for address candidates.
                score += (32 if has_pin else 0) + (18 if has_street else 0) + (10 if has_locality else 0)
            if name == "brand":
                if item.get("source_side") == "front" and item.get("region") == "logo":
                    score += 40
                elif item.get("source_side") == "back" and item.get("brand_corroborated"):
                    score += 15
                if item.get("brand_corroborated"):
                    score += 25
                score += min(12, 4 * sum(
                    1 for other in values
                    if _value_key(other["value"]) == _value_key(item["value"])
                ) - 4)
                # When one pass reads a short prefix and another reads a longer
                # compatible name, reward the supported complete candidate.
                normalized = re.sub(r"[^a-z0-9]", "", item["value"].casefold())
                for other in values:
                    candidate = re.sub(r"[^a-z0-9]", "", other["value"].casefold())
                    if normalized != candidate and len(normalized) >= 2 and candidate.startswith(normalized):
                        score -= 12
                    elif normalized != candidate and len(candidate) >= 3 and normalized.startswith(candidate):
                        score += min(15, len(normalized) - len(candidate))
            return score
        chosen = max(values if name == "brand" or name in {"manufacturer", "marketer", "packed_by", "imported_by"} else best_group, key=candidate_score)
        all_alternatives = []
        for item in values:
            if _value_key(item["value"]) != _value_key(chosen["value"]):
                all_alternatives.append(item["value"])
            all_alternatives.extend(item["alternatives"])
        alternatives = list(dict.fromkeys(value for value in all_alternatives if _value_key(value) != _value_key(chosen["value"])))
        review_reason = chosen["review_reason"]
        if name == "email" and alternatives and not review_reason:
            plausible_alternatives = [
                alternative for alternative in alternatives
                if re.fullmatch(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", alternative, re.I)
                and re.search(r"(?:consumer|customer|care|support)", alternative.split("@", 1)[0], re.I)
            ]
            if plausible_alternatives:
                review_reason = "Multiple plausible consumer-care email readings were found; verify the address."
        if name == "brand" and alternatives and not review_reason:
            # Keep all OCR alternatives for debugging, but only treat a
            # reading from a brand/logo region (or corroborated company name)
            # as a competing brand. Arbitrary package text is not evidence of
            # another brand.
            plausible_values = {
                _value_key(item["value"]) for item in values
                if float(item.get("confidence") or 0) >= 65
                and ((item.get("source_side") == "front" and item.get("region") == "logo")
                     or (item.get("source_side") == "back" and item.get("brand_corroborated")))
            }
            plausible_alternatives = [
                alternative for alternative in alternatives
                if _value_key(alternative) in plausible_values
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9&' -]{2,38}", alternative)
                and not re.search(r"\b(?:pack|packs|biscuits?|cookies?|butter|ingredients?)\b", alternative, re.I)
            ]
            if plausible_alternatives:
                review_reason = "Multiple plausible brand readings were found; verify the brand name."
        merged[name] = {**chosen, "alternatives": alternatives}
        merged[name]["review_reason"] = review_reason
        if name == "manufacturer" and not re.search(r"(?<!\d)[1-9]\d{2}\s?\d{3}(?!\d)", chosen["value"]):
            merged[name]["review_reason"] = review_reason or "Manufacturer address is incomplete or has no readable PIN code; verify the declaration."

    # Keep the two date roles from one OCR pass together. Independent
    # confidence ranking can otherwise combine half of one pass's reading with
    # half of another pass's swapped reading.
    if date_pairs:
        def date_pair_score(pair):
            score = sum(float(pair[name].get("confidence") or 0) for name in ("mfg_date", "expiry_date"))
            mfg_evidence = pair["mfg_date"].get("evidence", "")
            expiry_evidence = pair["expiry_date"].get("evidence", "")
            has_mfg = bool(re.search(r"\b(?:mfg|mfd|pkd)\b|\bmanufactur(?:ed|ing)\b|\bpacked\b|\bpacking\s+date\b", mfg_evidence, re.I))
            has_expiry = bool(re.search(r"\b(?:exp(?:iry)?|use\s*(?:by|before)|best\s*before)\b", expiry_evidence, re.I))
            wrong_mfg = bool(re.search(r"\b(?:exp(?:iry)?|use\s*(?:by|before)|best\s*before)\b", mfg_evidence, re.I))
            wrong_expiry = bool(re.search(r"\b(?:mfg|mfd|pkd)\b|\bmanufactur(?:ed|ing)\b|\bpacked\b|\bpacking\s+date\b", expiry_evidence, re.I))
            uncertainty_penalty = sum(
                30 for name in ("mfg_date", "expiry_date")
                if pair[name].get("review_reason")
            )
            return (score + (25 if has_mfg else 0) + (25 if has_expiry else 0)
                    - (40 if wrong_mfg else 0) - (40 if wrong_expiry else 0)
                    - uncertainty_penalty)

        by_pair = defaultdict(list)
        for pair in date_pairs:
            key = (_value_key(pair["mfg_date"]["value"]), _value_key(pair["expiry_date"]["value"]))
            by_pair[key].append(pair)
        best_group = max(
            by_pair.values(),
            key=lambda group: max(date_pair_score(item) for item in group) + min(6, 2 * (len(group) - 1)),
        )
        chosen = max(best_group, key=date_pair_score)
        competing = [group for group in by_pair.values() if group is not best_group]
        for name in ("mfg_date", "expiry_date"):
            field = chosen[name]
            alternatives = list(merged.get(name, {}).get("alternatives", []))
            alternatives.extend(
                group[0][name]["value"] for group in competing
                if _value_key(group[0][name]["value"]) != _value_key(field["value"])
            )
            alternatives = list(dict.fromkeys(value for value in alternatives if _value_key(value) != _value_key(field["value"])))
            reason = field.get("review_reason", "")
            if competing:
                reason = "OCR passes disagree on the manufacturing and use-by date association; verify both dates."
            merged[name] = {
                **field,
                "confidence": float(field.get("confidence") or 0),
                "source_image_id": chosen["source_image_id"],
                "source_pass": chosen["source_pass"],
                "attempt_confidence": chosen["attempt_confidence"],
                "alternatives": alternatives,
                "review_reason": reason,
            }
    return merged


def process_scan(scan):
    started = perf_counter()
    scan.extracted_fields.all().delete()
    field_candidates = []
    text_parts = []
    best_attempts = []
    barcode_values = []
    side_quantities = defaultdict(list)

    for scan_image in scan.images.all():
        stage_started = perf_counter()
        attempts = run_ocr(
            scan_image.image.path,
            focus_right=scan_image.side == "back",
            focus_logo=scan_image.side == "front",
        )
        logger.info("OCR stage scan=%s image=%s elapsed_seconds=%.3f", scan.pk, scan_image.pk, perf_counter() - stage_started)
        seen_texts = set()
        image_text_parts = []
        for attempt in attempts:
            text = attempt.get("text", "").strip()
            if not text:
                continue
            extraction_started = perf_counter()
            fields = extract_fields(text, ocr_lines=attempt.get("lines"))
            logger.info("Field extraction scan=%s image=%s pass=%s elapsed_seconds=%.3f", scan.pk, scan_image.pk, attempt.get("source_pass", attempt.get("variant", "")), perf_counter() - extraction_started)
            # Extraction functions use conservative field defaults when no line
            # confidence was supplied. In that case, carry this OCR pass's
            # confidence so competing passes are ranked by their actual read.
            has_line_confidence = any(
                line.get("confidence") is not None for line in attempt.get("lines", [])
            )
            for field in fields.values():
                if not has_line_confidence:
                    field["confidence"] = float(attempt.get("confidence") or field.get("confidence") or 0)
            field_candidates.append((fields, attempt, scan_image))
            if scan_image.side in {"front", "back"} and fields.get("net_quantity"):
                side_quantities[scan_image.side].append((
                    fields["net_quantity"]["value"],
                    float(fields["net_quantity"].get("confidence") or 0),
                    scan_image.pk, attempt.get("source_pass", attempt.get("variant", "")),
                    fields["net_quantity"].get("evidence", ""), fields["net_quantity"].get("review_reason", ""),
                    list(fields["net_quantity"].get("evidence_coordinates") or []),
                ))
            if text.casefold() not in seen_texts:
                text_parts.append(text)
                image_text_parts.append(text)
                seen_texts.add(text.casefold())
        scan_image.extracted_text = "\n\n".join(image_text_parts)
        best = attempts[0] if attempts else {}
        scan_image.ocr_confidence = float(best.get("confidence") or 0) if attempts else 0.0
        scan_image.ocr_source_pass = best.get("source_pass", "") if attempts else ""
        scan_image.save(update_fields=("extracted_text", "ocr_confidence", "ocr_source_pass"))
        if not attempts:
            continue
        best_attempts.append(best)
        if best.get("text", "").strip():
            text_parts.append(best["text"].strip())
        barcode_values.extend((item, scan_image.pk) for item in decode_barcodes(scan_image.image.path))

    combined_text = "\n\n".join(dict.fromkeys(text_parts))
    total_words = sum(max(0, item.get("word_count", 0)) for item in best_attempts)
    scan.ocr_confidence = (
        sum(item.get("confidence", 0) * max(0, item.get("word_count", 0)) for item in best_attempts) / total_words
        if total_words else 0.0
    )
    scan.extracted_text = combined_text
    scan.manually_verified = False
    scan.save(update_fields=("extracted_text", "ocr_confidence", "manually_verified"))

    stage_started = perf_counter()
    fields = _merge_fields(field_candidates)
    logger.info("Candidate selection and field extraction scan=%s elapsed_seconds=%.3f", scan.pk, perf_counter() - stage_started)
    barcode_candidate = next(((item, image_id) for item, image_id in barcode_values if len(item["value"]) <= 64), (None, None))
    barcode_record, barcode_image_id = barcode_candidate
    barcode = barcode_record["value"] if barcode_record else None
    known_product = Product.objects.filter(barcode=barcode).first() if barcode else None
    if barcode:
        fields["barcode"] = {
            "value": barcode, "confidence": 100, "source_image_id": barcode_image_id,
            "source_pass": "barcode_decoder", "evidence": "Barcode decoded from this source image.",
            "review_reason": "", "alternatives": [],
        }
    if known_product:
        fields.setdefault("generic_name", {
            "value": known_product.name,
            "confidence": 100,
            "source_image_id": barcode_image_id, "source_pass": "barcode_lookup",
            "evidence": "Product name matched from the decoded barcode.",
            "review_reason": "", "alternatives": [],
        })
        if known_product.brand:
            fields.setdefault("brand", {"value": known_product.brand, "confidence": 100})
    side_values = {}
    for side in ("front", "back"):
        values = side_quantities.get(side, [])
        if values:
            side_values[side] = max(values, key=lambda item: item[1])
    if side_values:
        best_side_value, best_side_confidence, image_id, source_pass, evidence, review_reason, evidence_coordinates = max(side_values.values(), key=lambda item: item[1])
        current_quantity = fields.get("net_quantity")
        if not current_quantity or best_side_confidence >= current_quantity["confidence"]:
            fields["net_quantity"] = {
                "value": best_side_value, "confidence": round(best_side_confidence, 2),
                "source_image_id": image_id, "source_pass": source_pass,
                "evidence": evidence, "evidence_coordinates": evidence_coordinates,
                "review_reason": review_reason, "alternatives": [],
            }
    front = side_values.get("front", (None, 0))[0]
    back = side_values.get("back", (None, 0))[0]
    if front and back and _value_key(front) != _value_key(back):
        fields["net_quantity_conflict"] = {
            "value": f"Front: {front}; back: {back}", "confidence": 100,
            "source_pass": "", "evidence": f"Front: {front}; back: {back}",
            "review_reason": "Front and back label quantity values differ.", "alternatives": [],
        }
    for name, result in fields.items():
        source_image_id = result.pop("source_image_id", None)
        source_pass = result.pop("source_pass", "")
        evidence = result.pop("evidence", "")
        evidence_coordinates = result.pop("evidence_coordinates", [])
        review_reason = result.pop("review_reason", "")
        alternatives = result.pop("alternatives", [])
        source_image = scan.images.filter(pk=source_image_id).first() if source_image_id else None
        source_text = source_image.extracted_text if source_image else scan.extracted_text
        matching_line = evidence or next((line.strip() for line in source_text.splitlines()
                                          if result["value"].casefold() in line.casefold()), "No matching OCR line was found.")
        ExtractedField.objects.update_or_create(
            scan=scan,
            field_name=name,
            defaults={
                "value": result["value"], "confidence": result["confidence"],
                "source_image_id": source_image_id, "source_type": ExtractedField.SourceType.OCR,
                "original_value": result["value"], "original_confidence": result["confidence"],
                "original_evidence": matching_line, "source_pass": source_pass,
                "original_evidence_coordinates": evidence_coordinates,
                "alternatives": alternatives, "review_reason": review_reason,
            },
        )

    product_name = fields.get("generic_name", {}).get("value") or "Unidentified product"
    brand = fields.get("brand", {}).get("value", "")
    if barcode:
        product, _ = Product.objects.get_or_create(
            barcode=barcode,
            defaults={"name": product_name, "brand": brand},
        )
        changed = []
        if product.name == "Unidentified product" and product_name != product.name:
            product.name = product_name
            changed.append("name")
        if not product.brand and brand:
            product.brand = brand
            changed.append("brand")
        if changed:
            product.save(update_fields=changed)
    elif scan.product_id:
        product = scan.product
        changed = []
        if product.name == "Unidentified product" and product_name != product.name:
            product.name = product_name
            changed.append("name")
        if not product.brand and brand:
            product.brand = brand
            changed.append("brand")
        if changed:
            product.save(update_fields=changed)
    else:
        product = Product.objects.create(name=product_name, brand=brand)
    if scan.product_id != product.pk:
        scan.product = product
        scan.save(update_fields=("product",))

    stage_started = perf_counter()
    evaluate(scan)
    logger.info("Compliance evaluation scan=%s elapsed_seconds=%.3f", scan.pk, perf_counter() - stage_started)
    logger.info("Total scan processing scan=%s elapsed_seconds=%.3f", scan.pk, perf_counter() - started)
    return scan
