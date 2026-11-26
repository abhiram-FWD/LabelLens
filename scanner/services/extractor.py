"""Best-effort extraction of declarations from noisy product-label OCR."""

import re
from functools import lru_cache
from pathlib import Path
from datetime import datetime

from rapidfuzz import fuzz, process


_MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_DATE = rf"(?:\d{{1,2}}\s*[/.-]\s*\d{{1,2}}\s*[/.-]\s*\d{{2,4}}|\d{{1,2}}\s*[/.-]\s*{_MONTH}\s*[/.-]\s*\d{{2,4}}|\d{{1,2}}\s*[/.-]\s*\d{{2,4}}|{_MONTH}\s+\d{{4}}|\d{{4}}\s*[/.-]\s*\d{{1,2}})"
_PIN = re.compile(r"(?<!\d)([1-9]\d{2})\s?(\d{3})(?!\d)")
_QUANTITY_LABEL = re.compile(
    r"\bn[e3]t\s*(?:weight|wt\.?|qty\.?|quantity|contents?|content)\b\s*[:#=\-]?\s*",
    re.I,
)
_QUANTITY_DECLARATION = re.compile(r"\bn[e3]t\s*(?:weight|wt\.?|qty\.?|quantity|contents?|content)\b", re.I)
_QUANTITY = re.compile(
    r"(?P<amount>\d+(?:[.,]\d{1,3})?)\s*(?P<unit>kg|mg|m[l1]|cl|g|l|units?|pcs?\.?|pieces?|count|ct|1|9)?\b",
    re.I,
)
_CITY_NAMES = (
    "Mumbai", "Pune", "Delhi", "Bengaluru", "Chennai", "Kolkata", "Ahmedabad",
    "Hyderabad", "Daman", "Nashik", "Nagpur", "Jaipur", "Lucknow", "Surat",
    "Indore", "Bhopal", "Vadodara", "Thane", "Mysuru", "Coimbatore",
)
_HEADER = re.compile(
    r"\b(?:net\s*(?:wt|weight|qty|quantity|content|contents)|mrp|m\.?r\.?p\.?|mfg|mfd|pkd|manufactur|"
    r"packed\s*(?:on|by)|best\s*before|expiry|expires|use\s*by|consumer\s*care|customer\s*care|"
    r"consumer\s+complaints|toll\s*free|manufactured\s*by|marketed\s*by|imported\s*by|made\s*in|"
    r"product\s*of|ingredients|barcode|batch\s*(?:no|number)|fssai|incl\.?\s*of\s*all\s*taxes|"
    r"for\s+feedback|complaints?|email|tel\.?)\b",
    re.I,
)
_PARTY_HEADERS = (
    ("manufacturer", re.compile(r"\b(?:manufactured|manufacturer|mfd\.?)\s*by\b\s*[:#-]?", re.I)),
    ("marketer", re.compile(r"\bmarketed\s*by\b\s*[:#-]?", re.I)),
    ("packed_by", re.compile(r"\b(?:packed|pkd\.?)\s*by\b\s*[:#-]?", re.I)),
    ("imported_by", re.compile(r"\bimported\s*by\b\s*[:#-]?", re.I)),
)
_COMMODITY_FILE = Path(__file__).resolve().parents[2] / "data" / "commodities.txt"
_COMMODITY_FUZZ_CUTOFF = 82
_GENERIC_PREFIX_NOISE = {"original", "classic", "premium", "fresh", "best", "the"}
_BRAND_NOISE = {
    "original", "classic", "premium", "ingredients", "nutrition", "information",
    "serving", "suggestion", "delicious", "tasty", "new", "product",
    "pack", "packs", "biscuits", "biscuit", "cookies", "cookie", "butter",
    "good", "day", "price", "only", "value", "family",
}


def _load_commodities():
    try:
        return tuple(
            line.strip() for line in _COMMODITY_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    except OSError:
        return ()


_COMMODITIES = _load_commodities()


def _generic_match(lines):
    """Find a commodity phrase and keep useful descriptor words before it."""
    candidates = []
    for line_index, line in enumerate(lines):
        if _HEADER.search(line) or len(line) > 80 or re.search(r"\d", line):
            continue
        words = re.findall(r"[A-Za-z][A-Za-z'-]*", line)
        for start in range(len(words)):
            for size in range(1, min(4, len(words) - start) + 1):
                phrase = " ".join(words[start:start + size])
                match = process.extractOne(
                    phrase.casefold(), _COMMODITIES, scorer=fuzz.ratio,
                    score_cutoff=_COMMODITY_FUZZ_CUTOFF,
                )
                if not match:
                    continue
                commodity, score, _ = match
                prefix = [word for word in words[:start] if word.casefold() not in _GENERIC_PREFIX_NOISE]
                # Keep at most two descriptor words; avoid pulling a tagline into the name.
                generic = " ".join(prefix[-2:] + commodity.title().split())
                candidates.append((score, size, -line_index, generic.strip(), phrase))
    if not candidates:
        return None, set()
    score, _size, _line, generic, phrase = max(candidates)
    matched_words = {word.casefold() for word in re.findall(r"[A-Za-z][A-Za-z'-]*", phrase)}
    return {"value": generic, "confidence": round(min(96, max(65, score)), 2)}, matched_words


def _brand_match(lines, generic_value, generic_words):
    """Use salient, early non-declaration text as a best-effort brand candidate."""
    generic_tokens = {word.casefold() for word in re.findall(r"[A-Za-z][A-Za-z'-]*", generic_value or "")}
    candidates = []
    for index, line in enumerate(lines):
        words = re.findall(r"[A-Za-z][A-Za-z0-9'-]*", line)
        if not words or len(words) > 4 or len(line) > 40 or _HEADER.search(line):
            continue
        folded = {word.casefold() for word in words}
        commodity_match = any(
            process.extractOne(" ".join(words[start:start + size]).casefold(), _COMMODITIES,
                               scorer=fuzz.ratio, score_cutoff=_COMMODITY_FUZZ_CUTOFF)
            for start in range(len(words))
            for size in range(1, min(4, len(words) - start) + 1)
        )
        if commodity_match or folded <= _BRAND_NOISE or folded & generic_tokens:
            continue
        if re.search(r"\d|\b(?:net|weight|qty|mrp|mfg|exp|batch|fssai|india|road|street|pin)\b", line, re.I):
            continue
        # Prefer the highest/earliest line, with a small boost for distinctive brand styling.
        style = 3 if "-" in line else 2 if line.isupper() else 0
        candidates.append((style - index * 0.25, _clean(line)))
    if not candidates:
        return None
    return {"value": max(candidates, key=lambda item: item[0])[1], "confidence": 68}


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip(" \t:;,-|.")


def _field(value, confidence, evidence="", review_reason="", alternatives=None):
    value = _clean(value)
    if not value:
        return None
    return {
        "value": value, "confidence": confidence, "evidence": evidence,
        "review_reason": review_reason, "alternatives": alternatives or [],
        "evidence_coordinates": [],
    }


def _fix_city_names(value):
    def fix(match):
        token = match.group(0)
        found = process.extractOne(token, _CITY_NAMES, scorer=fuzz.ratio, score_cutoff=82)
        return found[0] if found else token

    return re.sub(r"\b[A-Za-z]{4,}\b", fix, value)


def _normalize_quantity(amount, unit):
    amount = amount.replace(",", ".")
    unit = (unit or "").lower()
    if unit == "1":
        unit = "l"
    if unit == "m1":
        unit = "ml"
    if unit in {"unit", "units", "ct", "count", "piece", "pieces", "pc", "pcs"}:
        unit = "unit"
    if unit == "9":
        unit = "g"
    if not unit and "." not in amount and amount.endswith("9") and len(amount) >= 3:
        # A trailing 9 can stand in for a lost g only on an integer reading.
        amount, unit = amount[:-1], "g"
    return f"{amount} {unit}" if unit else amount


def _quantity_candidate(text):
    if not text or re.search(r"\b(?:mrp|unit\s*sale\s*price|batch|lot|fssai|exp(?:iry)?|use\s*by)\b", text, re.I):
        return None
    if any(_plausible_date(match.group(0)) for match in re.finditer(_DATE, text, re.I)):
        return None
    if _PIN.search(text) or re.search(r"\d{1,2}\s*[/.-]\s*\d{1,2}\s*[/.-]\s*\d{2,4}", text):
        return None
    match = _QUANTITY.search(text)
    if not match:
        return None
    amount = match.group("amount")
    unit = match.group("unit") or ""
    # Reject values embedded in longer identifiers or postal numbers.
    if len(amount.replace(".", "").replace(",", "")) > 7:
        return None
    return _normalize_quantity(amount, unit), bool(unit)


def _quantities(lines):
    candidates = []
    for index, line in enumerate(lines):
        for declaration in _QUANTITY_DECLARATION.finditer(line["text"]):
            inline = _quantity_candidate(line["text"][:declaration.start()]) or _quantity_candidate(line["text"][declaration.end():])
            if inline:
                candidates.append((inline[0], inline[1], index, index, 0))
                continue
            neighbors = []
            for neighbor_index in (index - 1, index + 1):
                if not 0 <= neighbor_index < len(lines):
                    continue
                candidate = _quantity_candidate(lines[neighbor_index]["text"])
                if candidate:
                    distance = _line_distance(lines[index], lines[neighbor_index], index, neighbor_index)
                    neighbors.append((distance, candidate, neighbor_index))
            if neighbors:
                distance, candidate, value_index = min(neighbors, key=lambda item: item[0])
                candidates.append((candidate[0], candidate[1], value_index, index, distance))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[4], not item[1]))
    selected = candidates[0]
    alternatives = list(dict.fromkeys(item[0] for item in candidates if item[0] != candidates[0][0]))
    review_reason = ""
    if len(alternatives) > 1 and candidates[1][4] == selected[4]:
        review_reason = "More than one nearby value could match the net quantity declaration."
    elif not selected[1]:
        review_reason = "The value is near the net quantity declaration, but its unit could not be read reliably."
    evidence_indexes = sorted({selected[2], selected[3]})
    return selected[0], evidence_indexes, review_reason, alternatives


def _line_distance(first, second, first_index, second_index):
    first_box = (first.get("left", 0), first.get("top", first_index), first.get("right", 0), first.get("bottom", first_index + 1))
    second_box = (second.get("left", 0), second.get("top", second_index), second.get("right", 0), second.get("bottom", second_index + 1))
    vertical_gap = max(0, first_box[1] - second_box[3], second_box[1] - first_box[3])
    if first.get("has_coordinates") and second.get("has_coordinates"):
        horizontal_gap = max(0, first_box[0] - second_box[2], second_box[0] - first_box[2])
        return vertical_gap + horizontal_gap * 0.15
    return abs(first_index - second_index) * 100


def _declaration_dates(lines):
    manufacturing_pattern = re.compile(
        r"\b(?:(?:mfg|mfd|pkd)\.?(?:\s+date)?|manufacturing\s+date|"
        r"manufactured(?:\s+(?:on|date))?|packed(?:\s+(?:on|date))?|packing\s+date)(?!\s+by)\b", re.I,
    )
    expiry_pattern = re.compile(r"\b(?:exp(?:iry)?(?:\s+date)?\.?|use\s+(?:by|before)|best\s+before(?:\s+end)?)\b", re.I)
    dates = []
    declarations = []
    for line_index, line in enumerate(lines):
        for match in re.finditer(_DATE, line["text"], re.I):
            if _plausible_date(match.group(0)):
                dates.append((line_index, match.group(0), match.start(), match.end()))
        for role, pattern in (("mfg_date", manufacturing_pattern), ("expiry_date", expiry_pattern)):
            match = pattern.search(line["text"])
            if match:
                declarations.append((line_index, role, match.start(), match.end()))
    if not declarations or not dates:
        return {}
    # Build date-to-heading edges first, then choose a minimum-distance
    # one-to-one matching. This lets the sequence Pkd/date/Use By/date pair up
    # globally instead of letting the middle date steal either heading.
    date_matches = sorted(dates, key=lambda item: (item[0], item[2]))
    declaration_matches = sorted(declarations, key=lambda item: (item[0], item[2]))
    edges = []
    for date_index, date_value, date_start, date_end in date_matches:
        choices = []
        for heading_position, (declaration_index, role, declaration_start, declaration_end) in enumerate(declaration_matches):
            if declaration_index == date_index:
                distance = 0
            else:
                distance = _line_distance(lines[date_index], lines[declaration_index], date_index, declaration_index)
                if not lines[date_index].get("has_coordinates") and abs(date_index - declaration_index) > 2:
                    continue
                if lines[date_index].get("has_coordinates") and distance > 120:
                    continue
            direction_tie = 0 if declaration_index <= date_index else 1
            choices.append((distance, direction_tie, heading_position, role))
        edges.append(sorted(choices))

    def best_matching(skip_date=None, reserved_mask=0):
        @lru_cache(maxsize=None)
        def solve(position, used_mask):
            if position >= len(date_matches):
                return (0, 0.0, ())
            if position == skip_date:
                return solve(position + 1, used_mask)
            best = solve(position + 1, used_mask)
            for distance, _direction, heading_position, role in edges[position]:
                bit = 1 << heading_position
                if used_mask & bit:
                    continue
                count, total, pairs = solve(position + 1, used_mask | bit)
                candidate = (count + 1, total + distance, ((position, heading_position),) + pairs)
                if candidate[0] > best[0] or (candidate[0] == best[0] and candidate[1] < best[1]):
                    best = candidate
            return best
        return solve(0, reserved_mask)

    matched_count, matched_distance, matching = best_matching()
    assigned = []
    for date_position, heading_position in matching:
        date_index, date_value, _date_start, _date_end = date_matches[date_position]
        declaration_index, role, _heading_start, _heading_end = declaration_matches[heading_position]
        distance = next(
            item[0] for item in edges[date_position] if item[2] == heading_position
        )
        tolerance = 12 if lines[date_index].get("has_coordinates") else 50
        ambiguous = False
        for alt_distance, _alt_direction, alt_heading, alt_role in edges[date_position]:
            if alt_heading == heading_position or alt_role == role:
                continue
            other_count, other_distance, _ = best_matching(
                skip_date=date_position, reserved_mask=1 << alt_heading,
            )
            if other_count + 1 == matched_count and alt_distance + other_distance <= matched_distance + tolerance:
                ambiguous = True
                break
        assigned.append((declaration_index, role, date_value, date_index, distance, ambiguous))
    fields = {}
    for declaration_index, role, value, date_index, distance, ambiguous in assigned:
        evidence = [lines[index]["text"] for index in sorted({date_index, declaration_index})]
        reason = ""
        if distance > 100:
            reason = "The date was not clearly associated with its declaration heading."
        if ambiguous:
            reason = "The nearby date could match more than one declaration; verify the date association."
        if role in fields and fields[role]["value"] != value:
            prior = fields[role]
            prior["review_reason"] = "Multiple different dates are near this declaration; verify the correct date."
            prior["alternatives"] = list(dict.fromkeys([*prior["alternatives"], prior["value"], value]))
            continue
        fields[role] = {
            "value": value, "evidence": "\n".join(evidence),
            "line_indexes": sorted({date_index, declaration_index}),
            "review_reason": reason, "alternatives": [],
        }
    return fields


def _plausible_date(value):
    month_year = re.fullmatch(r"(\d{1,2})\s*[/.-]\s*(\d{2}|\d{4})", value)
    if month_year:
        # Decimal OCR such as an MRP of 10.00 must not become a month/year.
        return 1 <= int(month_year.group(1)) <= 12 and int(month_year.group(2)) > 0
    day_month = re.fullmatch(r"(\d{1,2})\s*[/.-]\s*([A-Za-z]{3,9})\s*[/.-]\s*(\d{2,4})", value)
    day_numeric = re.fullmatch(r"(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{2,4})", value)
    try:
        if day_month:
            datetime.strptime(day_month.group(2)[:3], "%b")
            day = int(day_month.group(1))
        elif day_numeric:
            day = int(day_numeric.group(1))
            if not 1 <= int(day_numeric.group(2)) <= 12:
                return False
        else:
            return bool(re.fullmatch(r"[A-Za-z]{3,9}\s+\d{4}", value))
        return 1 <= day <= 31
    except ValueError:
        return False


def _mrp_values(text):
    amount = r"(?P<amount>\d[\d,]*(?:\.\d{1,2})?)"
    prefix = r"(?:Rs\.?|INR|[^\w\s])"
    pattern = re.compile(
        r"\b(?:m\s*\.?\s*r\s*\.?\s*p\.?|maximum\s*retail\s*price)\s*"
        rf"(?:{prefix}\s*){{0,2}}[:#=\-]?\s*{amount}",
        re.I,
    )
    values = []
    for match in pattern.finditer(text):
        values.append(match.group("amount").replace(",", ""))
    return values


def _date_value(text, label_pattern):
    match = re.search(r"(?:" + label_pattern + r")\s*[:#=-]?\s*(" + _DATE + r")", text, re.I)
    return match.group(1) if match else ""


def _clean_party_parts(parts):
    kept = []
    for index, part in enumerate(parts):
        value = _clean(part)
        if not value:
            continue
        if re.fullmatch(r"[\W\d_]+", value):
            continue
        # Keep source OCR intact, but avoid appending short isolated OCR
        # fragments from neighboring graphics to an address candidate.
        if re.fullmatch(r"[A-Z]{3,8}", value):
            continue
        if re.fullmatch(r"[A-Za-z]{1,2}", value) and value.casefold() not in {"st", "no", "uk", "us"}:
            continue
        if re.fullmatch(r"\d{1,2}", value):
            adjacent = " ".join(parts[max(0, index - 1):index + 2])
            if not re.search(r"\b(?:road|rd\.?|street|st\.?|lane|marg|avenue)\b", adjacent, re.I):
                continue
        kept.append(value)
    return kept


def _party_blocks(lines):
    blocks = {}
    active_name = None
    active_parts = []
    active_evidence = []
    def finish(name, parts, evidence):
        cleaned = _clean_party_parts(parts)
        if cleaned:
            blocks.setdefault(name, []).append(("; ".join(cleaned), cleaned, evidence))

    for line in lines:
        header = next(((name, match) for name, pattern in _PARTY_HEADERS if (match := pattern.search(line))), None)
        if header:
            if active_name:
                finish(active_name, active_parts, active_evidence)
            active_name, match = header
            active_parts = [_clean(line[match.end():])] if _clean(line[match.end():]) else []
            active_evidence = [line]
        elif active_name:
            # Date declarations are interleaved with responsible-party address
            # lines on compact panels. Keep scanning the party block, while
            # excluding the declaration and its value from the address.
            date_heading = re.search(
                r"\b(?:mfg|mfd|pkd|packed\s+on|packing\s+date|manufacturing\s+date|"
                r"use\s+by|use\s+before|best\s+before|exp(?:iry)?)\b", line, re.I,
            )
            if date_heading or re.fullmatch(rf"\s*{_DATE}\s*", line, re.I):
                continue
            # An inline date or price can be printed on the same line as an
            # address; remove the declaration value without losing the rest.
            line = re.sub(rf"\b{_DATE}\b", " ", line, flags=re.I)
            line = re.sub(r"\b(?:MRP|maximum\s+retail\s+price)\b.*$", "", line, flags=re.I)
            if not _clean(line):
                continue
            if _HEADER.search(line):
                finish(active_name, active_parts, active_evidence)
                active_name, active_parts, active_evidence = None, [], []
                continue
            active_parts.append(line)
            active_evidence.append(line)
    if active_name:
        finish(active_name, active_parts, active_evidence)
    return {
        name: {
            "value": _fix_city_names(_clean("; ".join(value for value, _parts, _evidence in values))),
            "evidence_lines": list(dict.fromkeys(line for _value, _parts, evidence in values for line in evidence)),
        }
        for name, values in blocks.items()
    }


def _normalize_email_text(text):
    text = re.sub(r"(?<=\w)\s*:\s*(?=[A-Za-z]{2,}\b)", ".", text)
    return re.sub(r"(?<=\w)\s*\.\s*(?=[A-Za-z]{2,}\b)", ".", text)


def _line_records(text, ocr_lines=None):
    text_lines = [_clean(line) for line in text.splitlines() if _clean(line)]
    records = []
    for index, line in enumerate(text_lines):
        supplied = ocr_lines[index] if ocr_lines and index < len(ocr_lines) else {}
        record = dict(supplied)
        record["text"] = line
        record["has_coordinates"] = all(key in supplied for key in ("left", "right", "top", "bottom"))
        records.append(record)
    return records


def _result_field(value, fallback_confidence, records, indexes=(), review_reason="", alternatives=None):
    indexes = sorted({index for index in indexes if 0 <= index < len(records)})
    evidence_lines = [records[index]["text"] for index in indexes]
    confidence_values = [float(records[index]["confidence"]) for index in indexes
                         if records[index].get("confidence") is not None]
    confidence = sum(confidence_values) / len(confidence_values) if confidence_values else fallback_confidence
    result = _field(value, round(confidence, 2), "\n".join(evidence_lines), review_reason, alternatives)
    result["evidence_coordinates"] = [
        {
            "text": records[index]["text"],
            "left": records[index]["left"], "top": records[index]["top"],
            "right": records[index]["right"], "bottom": records[index]["bottom"],
            "confidence": records[index].get("confidence"),
        }
        for index in indexes
        if all(records[index].get(key) is not None for key in ("left", "top", "right", "bottom"))
    ]
    return result


def _find_batch(lines):
    header = re.compile(r"\b(?:batch|lot)\b", re.I)
    candidates = []
    code_pattern = re.compile(r"\b[A-Z0-9][A-Z0-9/-]{2,}\b", re.I)
    for index, line in enumerate(lines):
        matches = list(header.finditer(line["text"]))
        for match in matches:
            inline_text = line["text"][match.end():]
            code = code_pattern.search(inline_text)
            evidence_indexes = [index]
            if not code:
                for nearby in (index - 1, index + 1):
                    if not 0 <= nearby < len(lines):
                        continue
                    candidates_on_line = list(code_pattern.finditer(lines[nearby]["text"]))
                    usable = [item for item in candidates_on_line
                              if len(item.group(0)) < 14 and not (item.group(0).isdigit() and _PIN.search(item.group(0)))
                              and not re.fullmatch(_DATE, item.group(0), re.I)]
                    if usable:
                        code = usable[-1] if nearby < index else usable[0]
                        evidence_indexes = [nearby, index]
                        break
            if code:
                value = code.group(0)
                if len(value) >= 14 or (value.isdigit() and _PIN.search(value)):
                    continue
                candidates.append((value, evidence_indexes, abs(index - evidence_indexes[0])))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[2])
    distinct = list(dict.fromkeys(item[0] for item in candidates))
    selected = candidates[0]
    reason = "" if len(distinct) == 1 else "More than one nearby identifier could match the batch number."
    return selected[0], selected[1], reason, distinct


def extract_fields(text, ocr_lines=None):
    """Extract normalized declarations, retaining conflict and auxiliary data."""
    if not text or not text.strip():
        return {}
    normalized = text.replace("\u00a0", " ").replace("â‚¹", "₹").replace("—", "- ")
    normalized = _fix_city_names(normalized)
    lines = _line_records(normalized, ocr_lines)
    line_texts = [line["text"] for line in lines]
    joined = "\n".join(line_texts)
    fields = {}

    quantity = _quantities(lines)
    if quantity:
        value, evidence_indexes, review_reason, alternatives = quantity
        fields["net_quantity"] = _result_field(value, 90, lines, evidence_indexes, review_reason, alternatives)

    mrps = _mrp_values(joined)
    if mrps:
        counts = {value: mrps.count(value) for value in set(mrps)}
        selected = max(counts, key=lambda value: (counts[value], -mrps.index(value)))
        mrp_line = next((index for index, line in enumerate(line_texts) if selected in line and re.search(r"\b(?:mrp|maximum\s+retail\s+price)", line, re.I)), None)
        evidence_indexes = [mrp_line] if mrp_line is not None else []
        fields["mrp"] = _result_field(selected, 88, lines, evidence_indexes)
        fields["mrp_currency"] = _result_field("INR", 80, lines, evidence_indexes)
    unit_price = re.search(
        r"\bunit\s*sale\s*price\b\s*[:#=-]?\s*(?:Rs\.?|INR|[^\w\s])?\s*"
        r"(\d[\d,]*(?:\.\d{1,2})?)(?:\s*/\s*(kg|g|l|ml))?",
        joined, re.I,
    )
    if unit_price:
        value = unit_price.group(1).replace(",", "")
        if unit_price.group(2):
            value += " / " + unit_price.group(2).lower()
        unit_price_line = next((index for index, line in enumerate(line_texts) if "unit sale price" in line.casefold()), None)
        fields["unit_sale_price"] = _result_field(value, 82, lines, [unit_price_line] if unit_price_line is not None else [])
    tax_lines = [index for index, line in enumerate(line_texts) if re.search(r"incl(?:usive)?\.?\s*(?:of\s*)?all\s*tax(?:es)?|all\s*tax(?:es)?\s*included", line, re.I)]
    if tax_lines:
        fields["mrp_inclusive_of_taxes"] = _result_field("Yes", 85, lines, tax_lines)

    for name, date_info in _declaration_dates(lines).items():
        fields[name] = _result_field(
            date_info["value"], 84, lines, date_info["line_indexes"],
            date_info["review_reason"], date_info["alternatives"],
        )

    parties = _party_blocks(line_texts)
    for name, party in parties.items():
        evidence_indexes = [index for index, line in enumerate(line_texts) if line in party["evidence_lines"]]
        fields[name] = _result_field(party["value"], 82, lines, evidence_indexes)
    pins = []
    for role, party in parties.items():
        role_pins = list(dict.fromkeys("".join(match.groups()) for match in _PIN.finditer(party["value"])))
        if role_pins:
            fields[f"{role}_pin"] = _result_field("; ".join(role_pins), 85, lines, [index for index, line in enumerate(line_texts) if line in party["evidence_lines"]])
            pins.extend(role_pins)
    if pins:
        pin_indexes = [index for index, line in enumerate(line_texts) if _PIN.search(line)]
        fields["pin_codes"] = _result_field("; ".join(dict.fromkeys(pins)), 85, lines, pin_indexes)
    care_context = re.search(r"\b(?:consumer\s*care|customer\s*care|consumer\s+complaints?|consumer\s+care\s+cell|toll\s*free|contact\s*(?:us|number)|tel\.?|email)\b", joined, re.I)
    email_text = re.sub(r"\bemail\s*:\s*", " ", joined, flags=re.I)
    email_text = _normalize_email_text(email_text)
    all_email_candidates = list(dict.fromkeys(
        match.group(0).rstrip(".,;:").lower()
        for match in re.finditer(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", email_text, re.I)
    ))
    email_candidates = [candidate for candidate in all_email_candidates
                        if care_context or re.search(r"(?:consumer|customer|care|support)", candidate.split("@", 1)[0], re.I)]
    def email_score(candidate):
        local = candidate.split("@", 1)[0].replace("_", ".").replace("-", ".")
        semantic = max(fuzz.ratio(local, phrase) for phrase in ("consumer.care", "customer.care", "consumer.support", "customer.support"))
        source_index = next((index for index, line in enumerate(line_texts) if candidate.casefold() in _normalize_email_text(line).casefold()), None)
        word_confidence = lines[source_index].get("confidence", 0) if source_index is not None else 0
        return semantic + float(word_confidence) * 0.08
    email = max(email_candidates, key=email_score) if email_candidates else ""
    email_indexes = [index for index, line in enumerate(line_texts) if email and email in _normalize_email_text(line).casefold()]
    phone_patterns = (
        r"(?<!\d)0(?:[\s-]?\d){10}(?!\d)",
        r"(?<!\d)(?:\+?91[\s-]?)?[6-9](?:[\s-]?\d){9}(?!\d)",
        r"\b1800[\s-]?\d{3}[\s-]?\d{4}\b",
    )
    phone = ""
    phone_display = ""
    for pattern in (phone_patterns if care_context else ()):
        match = re.search(pattern, joined)
        if match:
            phone_display = _clean(match.group(0))
            phone = re.sub(r"\D", "", match.group(0))
            break
    if phone:
        phone_indexes = [index for index, line in enumerate(line_texts) if phone_display and re.sub(r"\D", "", line).find(phone) >= 0]
        fields["phone"] = _result_field(phone, 82, lines, phone_indexes)
    if email:
        fields["email"] = _result_field(email, 82, lines, email_indexes, alternatives=[item for item in email_candidates if item != email])
    batch = _find_batch(lines)
    if batch:
        value, evidence_indexes, review_reason, alternatives = batch
        fields["batch_number"] = _result_field(value, 80, lines, evidence_indexes, review_reason, [item for item in alternatives if item != value])
    fssai_match = re.search(r"\bFSSAI\b[^\d]{0,25}(\d{14})", joined, re.I)
    if fssai_match:
        index = next((i for i, line in enumerate(line_texts) if "fssai" in line.casefold() and fssai_match.group(1) in line), None)
        fields["fssai_license"] = _result_field(fssai_match.group(1), 82, lines, [index] if index is not None else [])
    barcode_match = re.search(r"\bbarcode\b[^\d]{0,20}(\d{8,14})", joined, re.I)
    if barcode_match:
        index = next((i for i, line in enumerate(line_texts) if "barcode" in line.casefold() and barcode_match.group(1) in line), None)
        fields["barcode"] = _result_field(barcode_match.group(1), 85, lines, [index] if index is not None else [])

    origin_match = re.search(r"\b(?:made\s+in|product\s+of)\s+([A-Z][A-Za-z .'-]{1,40})", joined, re.I)
    if origin_match:
        origin = re.split(r"\b(?:batch|lot|mfg|manufactured|net|mrp|exp)\b", origin_match.group(1), flags=re.I)[0]
        index = next((i for i, line in enumerate(line_texts) if re.search(r"\b(?:made\s+in|product\s+of)\b", line, re.I)), None)
        fields["country_of_origin"] = _result_field(origin, 78, lines, [index] if index is not None else [])

    for line_index, line in enumerate(line_texts):
        if len(line) < 3 or len(line) > 80 or _HEADER.search(line):
            continue
        if _PIN.search(line) or re.search(r"\b(?:rs\.?|₹|\d{6}|\d+\s*(?:kg|g|mg|ml|l|cl)\b)", line, re.I):
            continue
        if re.fullmatch(r"[\W\d_]+", line):
            continue
        fields["generic_name"] = _result_field(line, 45, lines, [line_index])
        break
    generic, commodity_words = _generic_match(line_texts)
    if generic:
        line_index = next((index for index, line in enumerate(line_texts) if _HEADER.search(line) is None and any(word.casefold() in line.casefold() for word in commodity_words)), None)
        fields["generic_name"] = _result_field(generic["value"], generic["confidence"], lines, [line_index] if line_index is not None else [])
    else:
        fields.pop("generic_name", None)
    brand = _brand_match(line_texts, (generic or {}).get("value", ""), commodity_words)
    if brand:
        line_index = next((index for index, line in enumerate(line_texts) if line == brand["value"]), None)
        fields["brand"] = _result_field(brand["value"], brand["confidence"], lines, [line_index] if line_index is not None else [])
    return fields
