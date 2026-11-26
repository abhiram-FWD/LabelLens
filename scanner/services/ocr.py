"""Image preprocessing and diagnostic Tesseract OCR helpers."""

import logging
import os
from time import perf_counter

import cv2
import numpy as np
import pytesseract
from pytesseract import Output

logger = logging.getLogger(__name__)
PSM_MODES = (6, 11, 12)


def configure_tesseract():
    """Configure Tesseract from the environment and report startup failures."""
    command = os.environ.get("TESSERACT_CMD", "tesseract").strip() or "tesseract"
    pytesseract.pytesseract.tesseract_cmd = command
    logger.info("Tesseract command path: %s", command)
    try:
        version = pytesseract.get_tesseract_version()
    except Exception:
        logger.exception("Tesseract is not available at configured path %r", command)
        return False
    logger.info("Tesseract startup check passed: version %s", version)
    return True


def _load_image(image_path):
    image = cv2.imread(os.fspath(image_path))
    if image is None:
        raise ValueError(f"Could not read image: {image_path}")
    height, width = image.shape[:2]
    logger.info("OCR image %s has dimensions %sx%s", image_path, width, height)
    if max(height, width) > 3000:
        scale = 2500 / max(height, width)
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        logger.info("Resized large OCR image to %sx%s", image.shape[1], image.shape[0])
    return image


def _variants(image):
    """Return named OCR image variants, derived from the same resized source."""
    started = perf_counter()
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]
    upscale = cv2.resize(gray, (width * 2, height * 2), interpolation=cv2.INTER_CUBIC)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
    adaptive = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11
    )
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    blurred = cv2.GaussianBlur(gray, (0, 0), 2)
    sharpened = cv2.addWeighted(gray, 1.7, blurred, -0.7, 0)
    variants = [
        ("original", image),
        ("grayscale_2x", upscale),
        ("clahe", clahe),
        ("adaptive_threshold", adaptive),
        ("otsu_threshold", otsu),
        ("inverted", cv2.bitwise_not(otsu)),
        ("sharpened", sharpened),
    ]
    logger.info("OCR preprocessing stage elapsed_seconds=%.3f variants=%s", perf_counter() - started, len(variants))
    return variants


def preprocess(image_path):
    """Compatibility helper returning the adaptive-threshold variant."""
    image = _load_image(image_path)
    return dict(_variants(image))["adaptive_threshold"]


def _read_result(image, psm=6):
    started = perf_counter()
    data = pytesseract.image_to_data(
        image, config=f"--psm {psm}", output_type=Output.DICT
    )
    lines = {}
    confidences = []
    words = []
    texts = data.get("text", [])
    for index, (raw_word, raw_confidence) in enumerate(zip(texts, data.get("conf", []))):
        word = raw_word.strip() if raw_word else ""
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            confidence = -1
        if word:
            words.append(word)
            if confidence >= 0:
                confidences.append(confidence)
            key = tuple(data.get(name, [0] * len(texts))[index] for name in (
                "page_num", "block_num", "par_num", "line_num"
            ))
            left = int(data.get("left", [0] * len(texts))[index] or 0)
            top = int(data.get("top", [0] * len(texts))[index] or 0)
            width = int(data.get("width", [0] * len(texts))[index] or 0)
            height = int(data.get("height", [0] * len(texts))[index] or 0)
            lines.setdefault(key, []).append((left, top, width, height, word, confidence))
    line_items = list(lines.values())
    intervals = sorted((min(word[0] for word in line), max(word[0] + word[2] for word in line)) for line in line_items)
    gaps = [(intervals[index + 1][0] - intervals[index][1], intervals[index][1], intervals[index + 1][0])
            for index in range(len(intervals) - 1)]
    column_gap = max(gaps, default=(0, 0, 0))
    image_width = image.shape[1] if hasattr(image, "shape") else max((right for _left, right in intervals), default=0) + 1
    if column_gap[0] > max(30, image_width * 0.12):
        split = (column_gap[1] + column_gap[2]) / 2
        line_items.sort(key=lambda line: (
            0 if sum(word[0] for word in line) / len(line) < split else 1,
            min(word[1] for word in line), min(word[0] for word in line),
        ))
    else:
        line_items.sort(key=lambda line: (min(word[1] for word in line), min(word[0] for word in line)))
    ordered_lines = line_items
    line_details = []
    for line in ordered_lines:
        line = sorted(line, key=lambda word: word[0])
        line_text = " ".join(word[4] for word in line)
        valid_confidences = [word[5] for word in line if word[5] >= 0]
        line_details.append({
            "text": line_text,
            "confidence": sum(valid_confidences) / len(valid_confidences) if valid_confidences else 0.0,
            "left": min(word[0] for word in line),
            "top": min(word[1] for word in line),
            "right": max(word[0] + word[2] for word in line),
            "bottom": max(word[1] + word[3] for word in line),
            "word_confidences": [word[5] for word in line if word[5] >= 0],
        })
    text = "\n".join(line["text"] for line in line_details)
    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    logger.info("OCR recognition stage psm=%s elapsed_seconds=%.3f words=%s", psm, perf_counter() - started, len(words))
    return {"text": text, "confidence": mean_confidence, "word_count": len(words), "lines": line_details}


def _source_coordinates(lines, scale, offset_x=0, offset_y=0):
    """Convert OCR boxes from a scaled/cropped view back to source-image pixels."""
    for line in lines:
        for key, offset in (("left", offset_x), ("right", offset_x), ("top", offset_y), ("bottom", offset_y)):
            if key in line:
                line[key] = int(round(line[key] / scale + offset))


def iter_ocr_attempts(image_path, include_rotations=True, focus_right=False, focus_logo=False):
    """Yield every variant/PSM attempt for the debug command and OCR runner."""
    pytesseract.pytesseract.tesseract_cmd = os.environ.get("TESSERACT_CMD", "tesseract")
    image = _load_image(image_path)
    variant_started = perf_counter()
    generated = dict(_variants(image))
    # Keep useful full-label views and avoid repeating every PSM over every
    # preprocessing variant. Back labels also get a focused right-panel crop.
    selected_names = ("original", "clahe", "adaptive_threshold", "grayscale_2x")
    variants = [(name, generated[name]) for name in selected_names if name in generated]
    logger.info("OCR variant generation elapsed_seconds=%.3f", perf_counter() - variant_started)
    candidates = []
    source_height, source_width = image.shape[:2]
    for name, variant in variants:
        scale = variant.shape[1] / source_width
        views = [("full", variant, 0, 0)]
        if focus_right:
            height, width = variant.shape[:2]
            crop_left, crop_right = int(width * 0.50), int(width * 0.99)
            crop_top, crop_bottom = int(height * 0.10), int(height * 0.91)
            views.append(("right_panel", variant[crop_top:crop_bottom, crop_left:crop_right],
                          crop_left / scale, crop_top / scale))
        if focus_logo:
            height, width = variant.shape[:2]
            crop_left, crop_right = int(width * 0.08), int(width * 0.58)
            crop_top, crop_bottom = int(height * 0.15), int(height * 0.49)
            views.append(("logo", variant[crop_top:crop_bottom, crop_left:crop_right],
                          crop_left / scale, crop_top / scale))
        # The original and enlarged views are enough for a second look at the
        # declaration panel; skip redundant crops of every threshold variant.
        if name not in {"original", "grayscale_2x"}:
            views = [view for view in views if view[0] == "full"]
        for region, region_image, offset_x, offset_y in views:
          region_started = perf_counter()
          for psm in PSM_MODES:
            try:
                result = _read_result(region_image, psm)
            except Exception:
                logger.exception("OCR failed for %s (variant=%s, region=%s, psm=%s)", image_path, name, region, psm)
                yield {"variant": f"{name}_{region}", "region": region, "psm": psm, "error": True, "text": "", "confidence": 0.0, "word_count": 0}
                continue
            _source_coordinates(result.get("lines", []), scale, offset_x, offset_y)
            logger.info(
                "OCR attempt image=%s size=%sx%s variant=%s psm=%s confidence=%.2f words=%s",
                image_path, image.shape[1], image.shape[0], f"{name}_{region}", psm,
                result["confidence"], result["word_count"],
            )
            attempt = {"variant": f"{name}_{region}", "region": region,
                       "source_pass": f"{name}_{region}_psm{psm}", "psm": psm,
                       "image": region_image, **result}
            candidates.append((result["confidence"], result["word_count"]))
            yield attempt
          logger.info("OCR region stage region=%s variant=%s elapsed_seconds=%.3f", region, name, perf_counter() - region_started)

    best = max(candidates, default=None)
    minimum = float(os.environ.get("OCR_MIN_CONFIDENCE", "40"))
    if include_rotations and best and best[0] < minimum:
        for angle in (90, 180, 270):
            rotated = np.rot90(image, angle // 90).copy()
            rotated_variants = dict(_variants(rotated))
            for name in ("original", "adaptive_threshold"):
                if name not in rotated_variants:
                    continue
                variant = rotated_variants[name]
                for psm in (6,):
                    try:
                        result = _read_result(variant, psm)
                    except Exception:
                        logger.exception("OCR failed for %s (rotation=%s variant=%s psm=%s)", image_path, angle, name, psm)
                        yield {"variant": f"rotate_{angle}_{name}", "region": "full", "psm": psm, "error": True, "text": "", "confidence": 0.0, "word_count": 0}
                        continue
                    _source_coordinates(result.get("lines", []), 1.0)
                    logger.info(
                        "OCR attempt image=%s size=%sx%s rotation=%s variant=%s psm=%s confidence=%.2f words=%s",
                        image_path, image.shape[1], image.shape[0], angle, name, psm,
                        result["confidence"], result["word_count"],
                    )
                    yield {"variant": f"rotate_{angle}_{name}", "region": "full", "source_pass": f"rotate_{angle}_{name}_psm{psm}", "psm": psm, "image": variant, **result}


def run_ocr(image_path, focus_right=False, focus_logo=False):
    """Return the three strongest variant/PSM OCR attempts for one image."""
    command = os.environ.get("TESSERACT_CMD", "tesseract")
    pytesseract.pytesseract.tesseract_cmd = command
    logger.info("Starting OCR for %s with Tesseract command path %s", image_path, command)
    try:
        successful = []
        for attempt in iter_ocr_attempts(image_path, focus_right=focus_right, focus_logo=focus_logo):
            if attempt.get("error"):
                continue
            successful.append({
                key: value for key, value in attempt.items() if key != "image"
            })
    except Exception:
        logger.exception("Could not process OCR image %s (Tesseract path=%s)", image_path, command)
        return []
    if not successful:
        logger.error("All Tesseract OCR attempts failed for image %s (path=%s)", image_path, command)
        return []
    selection_started = perf_counter()
    successful.sort(key=lambda item: (item["confidence"], item["word_count"]), reverse=True)
    logger.info("OCR candidate selection elapsed_seconds=%.3f attempts=%s", perf_counter() - selection_started, len(successful))
    # Keep the strongest candidate from each requested region, plus a second
    # distinct reading where available. A global top-three cut discarded the
    # back label's right-side declaration panel in favor of left-side text.
    if any(item.get("region") for item in successful):
        selected = []
        for region in dict.fromkeys(item.get("region", "full") for item in successful):
            region_items = [item for item in successful if item.get("region", "full") == region]
            selected.extend(region_items[:2])
        return sorted(selected, key=lambda item: (item["confidence"], item["word_count"]), reverse=True)
    return successful[:3]
