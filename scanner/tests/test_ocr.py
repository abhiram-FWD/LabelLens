from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
import numpy as np
from PIL import Image
from pytesseract.pytesseract import TesseractNotFoundError

from scanner.services.extractor import extract_fields
from scanner.services.ocr import _load_image, _read_result, _variants, configure_tesseract, run_ocr
from scanner.services.pipeline import _merge_fields


class OCRFallbackTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.image_path = Path(self.temp_dir.name) / "label.png"
        Image.new("RGB", (40, 30), "white").save(self.image_path)
        self.addCleanup(self.temp_dir.cleanup)

    def test_source_image_is_resized_to_at_most_1600_pixels(self):
        large_path = Path(self.temp_dir.name) / "large.png"
        Image.new("RGB", (2400, 1200), "white").save(large_path)
        image = _load_image(large_path)
        self.assertLessEqual(max(image.shape[:2]), 1600)
        self.assertAlmostEqual(image.shape[1] / image.shape[0], 2.0, places=2)

    @patch.dict("os.environ", {"OCR_FAST_MODE": "true"})
    @patch("scanner.services.ocr._read_result")
    def test_fast_mode_uses_three_variants_two_psms_and_skips_focus(self, mock_ocr):
        mock_ocr.return_value = {"text": "readable", "confidence": 50, "word_count": 1, "lines": []}
        attempts = run_ocr(self.image_path, focus_right=True, focus_logo=True)
        self.assertEqual(mock_ocr.call_count, 6)
        self.assertEqual([call.args[1] for call in mock_ocr.call_args_list], [6, 11] * 3)
        self.assertEqual({attempt["variant"].split("_")[-1] for attempt in attempts}, {"full"})
        self.assertEqual({attempt["region"] for attempt in attempts}, {"full"})

    @patch.dict("os.environ", {"OCR_FAST_MODE": "1"})
    @patch("scanner.services.ocr._read_result", return_value={"text": "good", "confidence": 71, "word_count": 1, "lines": []})
    def test_fast_mode_stops_after_confidence_exceeds_70(self, mock_ocr):
        attempts = run_ocr(self.image_path)
        self.assertEqual(mock_ocr.call_count, 1)
        self.assertEqual(attempts[0]["confidence"], 71)

    @patch.dict("os.environ", {"OCR_FAST_MODE": "true", "OCR_TIME_BUDGET_SECONDS": "0.5"})
    @patch("scanner.services.ocr._variants", return_value=[("grayscale", np.zeros((30, 40), dtype=np.uint8)), ("otsu_threshold", np.zeros((30, 40), dtype=np.uint8)), ("clahe", np.zeros((30, 40), dtype=np.uint8))])
    @patch("scanner.services.ocr._read_result")
    def test_time_budget_stops_later_attempts_and_keeps_first_result(self, mock_ocr, *_mocks):
        def slow_result(*_args, **_kwargs):
            sleep(0.55)
            return {"text": "best so far", "confidence": 60, "word_count": 3, "lines": []}
        mock_ocr.side_effect = slow_result
        attempts = run_ocr(self.image_path)
        self.assertEqual(mock_ocr.call_count, 1)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["text"], "best so far")

    @staticmethod
    def result_data(words, confidences):
        count = len(words)
        return {
            "text": words, "conf": [str(value) for value in confidences],
            "page_num": [1] * count, "block_num": [1] * count,
            "par_num": [1] * count, "line_num": [1] * count,
        }

    @patch.dict("os.environ", {"TESSERACT_CMD": "mock-tesseract"})
    @patch("scanner.services.ocr._load_image", return_value=np.zeros((30, 40, 3), dtype=np.uint8))
    @patch("scanner.services.ocr._variants", return_value=[("original", np.zeros((30, 40), dtype=np.uint8)), ("clahe", np.zeros((30, 40), dtype=np.uint8))])
    @patch("scanner.services.ocr.PSM_MODES", (6,))
    @patch("scanner.services.ocr._read_result")
    def test_tries_fallback_variant_and_returns_highest_confidence(self, mock_ocr, *_mocks):
        mock_ocr.side_effect = [
            {"text": "original", "confidence": 30, "word_count": 1},
            {"text": "enhanced label", "confidence": 90, "word_count": 2},
        ]
        attempts = run_ocr(self.image_path)
        self.assertEqual(attempts[0]["text"], "enhanced label")
        self.assertEqual(attempts[0]["confidence"], 90)
        self.assertEqual(mock_ocr.call_count, 2)

    @patch("scanner.services.ocr.pytesseract.image_to_data")
    def test_ignores_tesseract_minus_one_confidences(self, mock_ocr):
        mock_ocr.return_value = self.result_data(["noise", "readable"], [-1, 80])
        result = _read_result(object())
        self.assertEqual(result["confidence"], 80)
        self.assertEqual(result["word_count"], 2)

    @patch("scanner.services.ocr.pytesseract.image_to_data")
    def test_rebuilds_lines_by_visual_position(self, mock_ocr):
        mock_ocr.return_value = {
            "text": ["right", "left", "column", "column"],
            "conf": [90] * 4, "page_num": [1] * 4,
            "block_num": [1] * 4, "par_num": [1] * 4,
            "line_num": [1, 1, 2, 2], "left": [300, 10, 300, 10],
            "top": [10, 10, 30, 30],
        }
        self.assertEqual(_read_result(object())["text"], "left right\ncolumn column")

    @patch("scanner.services.ocr.iter_ocr_attempts")
    def test_run_ocr_returns_top_three_attempts_by_confidence(self, mock_attempts):
        mock_attempts.return_value = [
            {"variant": "a", "psm": 6, "text": "low", "confidence": 20, "word_count": 2},
            {"variant": "b", "psm": 11, "text": "best", "confidence": 90, "word_count": 4},
            {"variant": "c", "psm": 12, "text": "mid", "confidence": 60, "word_count": 3},
            {"variant": "d", "psm": 6, "text": "second", "confidence": 80, "word_count": 5},
        ]
        attempts = run_ocr(self.image_path)
        self.assertEqual([item["text"] for item in attempts], ["best", "second", "mid"])

    @patch("scanner.services.ocr._load_image", return_value=np.zeros((30, 40, 3), dtype=np.uint8))
    @patch("scanner.services.ocr.PSM_MODES", (6,))
    @patch("scanner.services.ocr._read_result", return_value={"text": "label", "confidence": 80, "word_count": 1, "lines": []})
    def test_ocr_logs_stage_durations(self, _read, _load):
        with self.assertLogs("scanner.services.ocr", level="INFO") as captured:
            run_ocr(self.image_path)
        log_text = "\n".join(captured.output)
        self.assertIn("OCR preprocessing stage elapsed_seconds=", log_text)
        self.assertIn("OCR region stage region=full", log_text)
        self.assertIn("OCR candidate selection elapsed_seconds=", log_text)

    @patch("scanner.services.ocr._load_image", return_value=np.zeros((100, 200, 3), dtype=np.uint8))
    @patch("scanner.services.ocr._variants", return_value=[("original", np.zeros((100, 200), dtype=np.uint8))])
    @patch("scanner.services.ocr.PSM_MODES", (6,))
    @patch("scanner.services.ocr._read_result")
    def test_back_scan_runs_full_and_right_panel_and_keeps_source_coordinates(self, mock_ocr, *_mocks):
        mock_ocr.side_effect = [
            {"text": "full", "confidence": 70, "word_count": 1, "lines": []},
            {"text": "panel", "confidence": 90, "word_count": 1,
             "lines": [{"left": 10, "right": 30, "top": 5, "bottom": 15}]},
        ]
        attempts = run_ocr(self.image_path, focus_right=True)
        self.assertEqual({item["region"] for item in attempts}, {"full", "right_panel"})
        panel = next(item for item in attempts if item["region"] == "right_panel")
        self.assertGreaterEqual(panel["lines"][0]["left"], 100)
        self.assertEqual(panel["source_pass"], "original_right_panel_psm6")

    @patch.dict("os.environ", {"OCR_MIN_CONFIDENCE": "40"})
    @patch("scanner.services.ocr._load_image", return_value=np.zeros((30, 40, 3), dtype=np.uint8))
    @patch("scanner.services.ocr._variants", return_value=[("original", np.zeros((30, 40), dtype=np.uint8))])
    @patch("scanner.services.ocr.PSM_MODES", (6,))
    @patch("scanner.services.ocr._read_result")
    def test_rotates_when_confidence_is_low(self, mock_ocr, *_mocks):
        mock_ocr.side_effect = [
            {"text": "poor", "confidence": 10, "word_count": 1},
            {"text": "poor", "confidence": 10, "word_count": 1},
            {"text": "poor", "confidence": 10, "word_count": 1},
            {"text": "rotated readable package text", "confidence": 85, "word_count": 5},
            {"text": "other", "confidence": 20, "word_count": 1},
            {"text": "other", "confidence": 20, "word_count": 1},
        ]
        attempts = run_ocr(self.image_path)
        self.assertEqual(attempts[0]["text"], "rotated readable package text")
        self.assertEqual(attempts[0]["confidence"], 85)
        self.assertEqual(mock_ocr.call_count, 4)

    @patch("scanner.services.ocr._load_image", return_value=np.zeros((30, 40, 3), dtype=np.uint8))
    @patch("scanner.services.ocr._variants", return_value=[("original", np.zeros((30, 40), dtype=np.uint8))])
    @patch("scanner.services.ocr.PSM_MODES", (6,))
    @patch("scanner.services.ocr._read_result", side_effect=TesseractNotFoundError())
    def test_missing_tesseract_returns_empty_result_after_logging(self, _mock_ocr, *_mocks):
        self.assertEqual(run_ocr(self.image_path), [])


class Scan9ImageOCRTests(SimpleTestCase):
    def test_scan9_front_and_back_images_extract_declaration_panel_and_brand(self):
        asset_dir = Path(__file__).resolve().parents[2] / "media" / "scans" / "2026" / "11"
        front = asset_dir / "labellens_mock_front.png"
        back = asset_dir / "labellens_mock_back.png"
        if not front.is_file() or not back.is_file():
            self.skipTest("Scan #9 image assets are not available in this checkout.")
        if not configure_tesseract():
            self.skipTest("Tesseract is not installed; image-level OCR verification is unavailable.")

        candidates = []
        for image_id, path, side in ((1, front, "front"), (2, back, "back")):
            attempts = run_ocr(
                path, focus_right=side == "back", focus_logo=side == "front",
            )
            self.assertTrue(attempts, f"No OCR attempts were returned for the {side} image.")
            for attempt in attempts:
                fields = extract_fields(attempt["text"], ocr_lines=attempt.get("lines"))
                candidates.append((fields, attempt, SimpleNamespace(pk=image_id)))

        fields = _merge_fields(candidates)
        self.assertEqual(fields["brand"]["value"].casefold(), "britannia")
        self.assertNotEqual(fields["brand"]["value"].casefold(), "pack")
        self.assertEqual(fields["net_quantity"]["value"], "75 g")
        self.assertEqual(fields["mrp"]["value"], "10.00")
        self.assertEqual(fields["mrp_currency"]["value"], "INR")
        self.assertEqual(fields["mrp_inclusive_of_taxes"]["value"], "Yes")
        self.assertEqual(fields["unit_sale_price"]["value"], "0.13 / g")
        self.assertEqual(fields["batch_number"]["value"], "B256A")
        self.assertEqual(fields["mfg_date"]["value"], "10/SEP/2026")
        self.assertEqual(fields["expiry_date"]["value"], "09/MAR/2027")
        self.assertEqual(fields["mrp_inclusive_of_taxes"]["value"], "Yes")
        self.assertEqual(fields["mfg_date"]["review_reason"], "")
        self.assertEqual(fields["expiry_date"]["review_reason"], "")
        for name, heading in (
            ("net_quantity", "Net Quantity"), ("mrp", "MRP"),
            ("unit_sale_price", "Unit Sale Price"), ("batch_number", "Batch No"),
            ("mfg_date", "Pkd"), ("expiry_date", "Use By"),
        ):
            self.assertIn(heading.casefold(), fields[name]["evidence"].casefold())
            self.assertTrue(fields[name]["source_pass"])
            self.assertEqual(fields[name]["source_image_id"], 2)
            self.assertTrue(fields[name]["evidence_coordinates"])
        self.assertIn(fields["brand"]["source_image_id"], {1, 2})
        self.assertIn("incl. of all taxes", fields["mrp_inclusive_of_taxes"]["evidence"].casefold())
