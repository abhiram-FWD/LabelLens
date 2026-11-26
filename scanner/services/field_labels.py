"""Human readable labels for stored extraction field identifiers."""

FIELD_LABELS = {
    "batch_number": "Batch number",
    "mrp_inclusive_of_taxes": "MRP inclusive of taxes",
    "net_quantity": "Net quantity",
    "generic_name": "Generic name",
    "manufacturer_name": "Manufacturer name",
    "manufacturer": "Manufacturer",
    "manufacturer_address": "Manufacturer address",
    "marketer_name": "Marketer name",
    "marketer": "Marketer",
    "packed_by": "Packer",
    "imported_by": "Importer",
    "expiry_date": "Expiry / Use-by date",
    "mfg_date": "Manufacturing / Packing date",
    "manufacturing_date": "Manufacturing / Packing date",
    "pkd_date": "Packed date",
    "packed_on": "Packed date",
    "consumer_care_phone": "Consumer care phone",
    "consumer_care_email": "Consumer care email",
    "consumer_care": "Consumer care details",
    "country_of_origin": "Country of origin",
    "mrp": "Maximum retail price (MRP)",
    "fssai_license": "FSSAI license number",
    "net_quantity_conflict": "Net quantity difference",
    "net_quantities": "Net quantities",
    "net_quantity_front": "Front net quantity",
    "net_quantity_back": "Back net quantity",
    "manufacturer_pin": "Manufacturer PIN code",
    "marketer_pin": "Marketer PIN code",
    "packed_by_pin": "Packer PIN code",
    "imported_by_pin": "Importer PIN code",
    "mfg_date": "Manufacturing date",
}


def field_label(field_name):
    """Return a stable display label without changing the stored field name."""
    import re

    key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(field_name or "").strip().replace("-", "_")).casefold()
    if key in FIELD_LABELS:
        return FIELD_LABELS[key]
    return " ".join(part.capitalize() for part in key.split("_") if part)


def display_ocr_text(text):
    """Filter blank and obvious OCR noise lines for presentation only."""
    import re

    meaningful_short = re.compile(
        r"^(?:₹|\$|£|€|rs\.?|mrp|mfd\.?|pkd\.?|g|kg|mg|l|ml|cl|pcs?\.?|ea|no\.?|\d{1,2}(?:[.,]\d{1,2})?|(?=[a-z0-9]{2}$)(?=.*[a-z])(?=.*\d)[a-z0-9]{2})$",
        re.IGNORECASE,
    )
    lines = []
    for raw_line in (text or "").splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        if len(line) <= 2 and not meaningful_short.fullmatch(line):
            continue
        lines.append(line)
    return "\n".join(lines)
