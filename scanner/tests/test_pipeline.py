from io import BytesIO
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image

from scanner.models import ExtractedField, Product, Rule, RuleResult, Scan, ScanImage
from scanner.services.pipeline import _merge_fields, process_scan


class ScanPipelineMergeTests(TestCase):
    def setUp(self):
        self.media_dir = TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.media_dir.cleanup)
        self.user = get_user_model().objects.create_user(username="pipeline-test")

    @staticmethod
    def image_upload(name):
        data = BytesIO()
        Image.new("RGB", (20, 20), "white").save(data, format="PNG")
        return SimpleUploadedFile(name, data.getvalue(), content_type="image/png")

    @staticmethod
    def attempt(text, confidence, word_count):
        return [{
            "variant": "test", "psm": 6, "text": text,
            "confidence": confidence, "word_count": word_count,
        }]

    def test_brand_merge_prefers_complete_hyphenated_candidate_and_keeps_truncated_alternative(self):
        merged = _merge_fields([
            ({"brand": {"value": "PAR", "confidence": 95, "evidence": "PAR"}},
             {"confidence": 95, "source_pass": "full_psm6"}),
            ({"brand": {"value": "Parle-G", "confidence": 82, "evidence": "Parle-G"}},
             {"confidence": 82, "source_pass": "right_psm11"}),
        ])
        self.assertEqual(merged["brand"]["value"], "Parle-G")
        self.assertIn("PAR", merged["brand"]["alternatives"])

    def test_manufacturer_complete_address_beats_higher_confidence_incomplete_read(self):
        merged = _merge_fields([
            ({"manufacturer": {"value": "Acme Ltd; Plot 1, Sector 2; 10/SEP/2026", "confidence": 98,
                               "evidence": "Manufactured by: Acme Ltd; Plot 1, Sector 2; 10/SEP/2026"}},
             {"confidence": 98, "source_pass": "full"}),
            ({"manufacturer": {"value": "Acme Ltd; Plot 1, Sector 2, North City 263153, India", "confidence": 72,
                               "evidence": "Manufactured by: Acme Ltd; Plot 1, Sector 2, North City 263153, India"}},
             {"confidence": 72, "source_pass": "panel"}),
        ])
        self.assertIn("263153", merged["manufacturer"]["value"])
        self.assertNotIn("10/SEP/2026", merged["manufacturer"]["value"])
        self.assertIn("10/SEP/2026", merged["manufacturer"]["alternatives"][0])
        self.assertFalse(merged["manufacturer"]["review_reason"])

    def test_manufacturer_without_complete_pin_address_needs_review(self):
        merged = _merge_fields([(
            {"manufacturer": {"value": "Acme Foods; Main Road", "confidence": 90,
                              "evidence": "Manufactured by: Acme Foods; Main Road"}},
            {"confidence": 90, "source_pass": "full"},
        )])
        self.assertIn("incomplete", merged["manufacturer"]["review_reason"].casefold())

    def test_brand_noise_is_retained_but_does_not_create_uncertainty(self):
        front = SimpleNamespace(pk=1, side="front")
        merged = _merge_fields([
            ({"brand": {"value": "BRITANNIA", "confidence": 70, "evidence": "BRITANNIA"}},
             {"confidence": 70, "source_pass": "logo_crop", "region": "logo"}, front),
            ({"brand": {"value": "SOR", "confidence": 96, "evidence": "SOR"}},
             {"confidence": 96, "source_pass": "full_psm6", "region": "full"}, front),
            ({"brand": {"value": "PACK", "confidence": 99, "evidence": "₹10 PACK"}},
             {"confidence": 99, "source_pass": "full_psm6", "region": "full"}, front),
        ])
        self.assertEqual(merged["brand"]["value"], "BRITANNIA")
        self.assertFalse(merged["brand"]["review_reason"])
        self.assertIn("SOR", merged["brand"]["alternatives"])

    def test_distinct_logo_region_brand_readings_remain_uncertain(self):
        front = SimpleNamespace(pk=1, side="front")
        merged = _merge_fields([
            ({"brand": {"value": "ACME", "confidence": 85, "evidence": "ACME"}},
             {"confidence": 85, "source_pass": "logo_a", "region": "logo"}, front),
            ({"brand": {"value": "NOVA", "confidence": 84, "evidence": "NOVA"}},
             {"confidence": 84, "source_pass": "logo_b", "region": "logo"}, front),
        ])
        self.assertTrue(merged["brand"]["review_reason"])

    def test_date_pair_merge_keeps_heading_supported_mapping_and_flags_conflict(self):
        correct = {
            "mfg_date": {"value": "10/SEP/2026", "confidence": 82, "evidence": "Pkd: 10/SEP/2026"},
            "expiry_date": {"value": "09/MAR/2027", "confidence": 82, "evidence": "Use By: 09/MAR/2027"},
        }
        swapped = {
            "mfg_date": {"value": "09/MAR/2027", "confidence": 96, "evidence": "Use By: 09/MAR/2027"},
            "expiry_date": {"value": "10/SEP/2026", "confidence": 96, "evidence": "Pkd: 10/SEP/2026"},
        }
        merged = _merge_fields([
            (correct, {"confidence": 82, "source_pass": "full_psm6"}),
            (swapped, {"confidence": 96, "source_pass": "right_psm11"}),
        ])
        self.assertEqual(merged["mfg_date"]["value"], "10/SEP/2026")
        self.assertEqual(merged["expiry_date"]["value"], "09/MAR/2027")
        self.assertTrue(merged["mfg_date"]["review_reason"])
        self.assertIn("09/MAR/2027", merged["mfg_date"]["alternatives"])

    def test_email_merge_preserves_plausible_alternatives_and_requests_verification(self):
        merged = _merge_fields([(
            {"email": {
                "value": "consumer.care@parle.biz", "confidence": 88,
                "evidence": "consumer.care@parle.biz",
                "alternatives": ["sonsumer.care@parle.biz", "im.consumer.care@parle.biz"],
            }},
            {"confidence": 88, "source_pass": "back_psm6"},
        )])
        self.assertEqual(merged["email"]["value"], "consumer.care@parle.biz")
        self.assertEqual(set(merged["email"]["alternatives"]), {
            "sonsumer.care@parle.biz", "im.consumer.care@parle.biz",
        })
        self.assertIn("verify", merged["email"]["review_reason"].casefold())

    @patch("scanner.services.pipeline.decode_barcodes", return_value=[])
    @patch("scanner.services.pipeline.run_ocr")
    def test_combines_confidence_across_images_and_flags_front_back_quantity_conflict(self, mock_ocr, _barcodes):
        scan = Scan.objects.create(user=self.user)
        ScanImage.objects.create(scan=scan, side=ScanImage.Side.FRONT, image=self.image_upload("front.png"))
        ScanImage.objects.create(scan=scan, side=ScanImage.Side.BACK, image=self.image_upload("back.png"))
        mock_ocr.side_effect = [
            self.attempt("Parle-G\nNET WEIGHT: 75 g", 50, 2),
            self.attempt("PKD: 10/2024\nNET WEIGHT: 715g\nBATCH NO: G241010", 90, 8),
        ]

        process_scan(scan)
        scan.refresh_from_db()
        fields = {item.field_name: item.value for item in ExtractedField.objects.filter(scan=scan)}
        self.assertEqual(scan.ocr_confidence, 82)
        self.assertIn("Parle-G", scan.extracted_text)
        self.assertIn("PKD: 10/2024", scan.extracted_text)
        self.assertEqual(fields["net_quantity"], "715 g")
        self.assertNotIn("net_quantity_front", fields)
        self.assertNotIn("net_quantity_back", fields)
        self.assertEqual(fields["net_quantity_conflict"], "Front: 75 g; back: 715 g")

    @patch("scanner.services.pipeline.decode_barcodes", return_value=[{"value": "8901719110102", "type": "EAN13"}])
    @patch("scanner.services.pipeline.run_ocr")
    def test_detects_parle_g_brand_and_generic_name_with_barcode_evidence(self, mock_ocr, _barcodes):
        scan = Scan.objects.create(user=self.user)
        ScanImage.objects.create(scan=scan, image=self.image_upload("parle.png"))
        mock_ocr.return_value = self.attempt(
            "Original Gluco Biscuits\nParle-G\nNET WEIGHT: 75 g", 92, 8,
        )

        process_scan(scan)

        scan.refresh_from_db()
        fields = {item.field_name: item.value for item in ExtractedField.objects.filter(scan=scan)}
        self.assertEqual(fields["generic_name"], "Gluco Biscuits")
        self.assertEqual(fields["brand"], "Parle-G")
        self.assertEqual(fields["barcode"], "8901719110102")
        self.assertEqual(scan.product.name, "Gluco Biscuits")
        self.assertEqual(scan.product.brand, "Parle-G")

    @patch("scanner.services.pipeline.decode_barcodes", return_value=[])
    @patch("scanner.services.pipeline.run_ocr")
    def test_unmatched_text_uses_unidentified_product_fallback(self, mock_ocr, _barcodes):
        scan = Scan.objects.create(user=self.user)
        ScanImage.objects.create(scan=scan, image=self.image_upload("unknown.png"))
        mock_ocr.return_value = self.attempt("Colorful family moments on every day", 90, 6)

        process_scan(scan)

        scan.refresh_from_db()
        self.assertEqual(scan.product.name, "Unidentified product")
        self.assertFalse(ExtractedField.objects.filter(scan=scan, field_name="generic_name").exists())

    @patch("scanner.services.pipeline.decode_barcodes", return_value=[])
    @patch("scanner.services.pipeline.run_ocr")
    def test_blurry_low_confidence_photo_is_saved_as_needs_review(self, mock_ocr, _barcodes):
        scan = Scan.objects.create(user=self.user)
        ScanImage.objects.create(scan=scan, side=ScanImage.Side.BACK, image=self.image_upload("blurry.png"))
        mock_ocr.return_value = self.attempt("faint package words too blurry", 18, 5)

        process_scan(scan)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Scan.Status.NEEDS_REVIEW)
        self.assertIsNone(scan.score)
        self.assertEqual(scan.ocr_confidence, 18)

    @patch("scanner.services.pipeline.decode_barcodes", return_value=[])
    @patch("scanner.services.pipeline.run_ocr")
    def test_rule_evidence_uses_the_image_that_supplied_the_field(self, mock_ocr, _barcodes):
        scan = Scan.objects.create(user=self.user)
        front = ScanImage.objects.create(scan=scan, side=ScanImage.Side.FRONT, image=self.image_upload("source-front.png"))
        back = ScanImage.objects.create(scan=scan, side=ScanImage.Side.BACK, image=self.image_upload("source-back.png"))
        Rule.objects.create(code="manufacturer_address", name="Manufacturer details")
        mock_ocr.side_effect = [
            self.attempt("NET QUANTITY: 500 g", 91, 3),
            self.attempt("Manufactured by: North Foods\n12 Main Road, Delhi 110001", 89, 9),
        ]
        process_scan(scan)
        manufacturer = scan.extracted_fields.get(field_name="manufacturer")
        result = scan.rule_results.get(rule__code="manufacturer_address")
        self.assertEqual(manufacturer.source_image_id, back.pk)
        self.assertEqual(result.source_image_id, back.pk)
        self.assertEqual(result.ocr_confidence, 89)
        self.assertIn("Manufactured by", result.matched_line)
        self.assertIn("12 Main Road", result.following_line)

    @patch("scanner.services.pipeline.decode_barcodes", return_value=[])
    @patch("scanner.services.pipeline.run_ocr")
    def test_exact_sample_fields_keep_line_confidence_and_original_ocr_evidence(self, mock_ocr, _barcodes):
        scan = Scan.objects.create(user=self.user)
        ScanImage.objects.create(scan=scan, side=ScanImage.Side.BACK, image=self.image_upload("sample.png"))
        text = "MRP & 10.00 (INCL. OF ALL TAXES)\nPkd.: 10/SEP/2026\nUse By: 09/MAR/2027\nNET QUANTITY : 79.49"
        mock_ocr.return_value = [{
            "variant": "original_right", "source_pass": "original_right_psm6",
            "psm": 6, "text": text, "confidence": 90, "word_count": 15,
            "lines": [
                {"text": "MRP & 10.00 (INCL. OF ALL TAXES)", "confidence": 91, "left": 10, "top": 10, "right": 220, "bottom": 25},
                {"text": "Pkd.: 10/SEP/2026", "confidence": 88, "left": 10, "top": 30, "right": 180, "bottom": 45},
                {"text": "Use By: 09/MAR/2027", "confidence": 93, "left": 10, "top": 50, "right": 180, "bottom": 65},
                {"text": "NET QUANTITY : 79.49", "confidence": 72, "left": 10, "top": 70, "right": 180, "bottom": 85},
            ],
        }]

        process_scan(scan)

        fields = {field.field_name: field for field in ExtractedField.objects.filter(scan=scan)}
        self.assertEqual(fields["mrp"].value, "10.00")
        self.assertEqual(fields["mfg_date"].value, "10/SEP/2026")
        self.assertEqual(fields["expiry_date"].value, "09/MAR/2027")
        self.assertEqual(fields["net_quantity"].value, "79.49")
        self.assertEqual(fields["net_quantity"].confidence, 72)
        self.assertTrue(fields["net_quantity"].review_reason)
        self.assertEqual(fields["net_quantity"].source_pass, "original_right_psm6")
        self.assertIn("NET QUANTITY", fields["net_quantity"].original_evidence)
        self.assertEqual(fields["net_quantity"].source_image_id, scan.images.get(side=ScanImage.Side.BACK).pk)
        self.assertEqual(fields["net_quantity"].original_evidence_coordinates[0]["left"], 10)
        self.assertEqual(fields["net_quantity"].original_evidence_coordinates[0]["top"], 70)
        back = scan.images.get(side=ScanImage.Side.BACK)
        self.assertEqual(back.ocr_confidence, 90)
        self.assertEqual(back.ocr_source_pass, "original_right_psm6")
