"""Barcode and QR decoding helpers powered by pyzbar."""


def decode_barcodes(image_path):
    """Return unique decoded values and symbologies from an image.

    An unavailable native zbar library or an unreadable image yields an empty
    list so a barcode decoding problem does not prevent an OCR scan.
    """
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode

        with Image.open(image_path) as image:
            decoded = decode(image)
    except (ImportError, OSError, ValueError):
        return []

    results = []
    seen = set()
    for item in decoded:
        value = item.data.decode("utf-8", errors="replace").strip()
        if value and value not in seen:
            seen.add(value)
            results.append({"value": value, "type": item.type})
    return results
