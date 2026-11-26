from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image
from rest_framework.test import APIClient

from scanner.models import ExtractedField, Rule, RuleResult, Scan, ScanImage


def image_upload(name="label.png", fmt="PNG", color="white"):
    buffer = BytesIO()
    Image.new("RGB", (40, 30), color).save(buffer, format=fmt)
    content_type = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}[fmt]
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=content_type)


class UploadValidationAndAPITests(TestCase):
    def setUp(self):
        self.temp_media = TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.temp_media.name)
        self.media_override.enable()
        self.user = get_user_model().objects.create_user(username="api-user", password="test-password")
        self.api = APIClient()

    def tearDown(self):
        self.media_override.disable()
        self.temp_media.cleanup()

    def test_api_requires_authentication(self):
        response = self.api.post("/api/scan/", {}, format="multipart")
        self.assertIn(response.status_code, (401, 403))

    def test_api_rejects_wrong_type(self):
        self.api.force_authenticate(self.user)
        response = self.api.post("/api/scan/", {"images": image_upload("label.gif", "GIF")}, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertIn("JPEG, PNG, or WEBP", str(response.data))
        self.assertEqual(Scan.objects.count(), 0)

    def test_api_rejects_oversized_file(self):
        self.api.force_authenticate(self.user)
        upload = SimpleUploadedFile("large.png", b"x" * (5 * 1024 * 1024 + 1), content_type="image/png")
        response = self.api.post("/api/scan/", {"images": upload}, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertIn("larger than 5 MB", str(response.data))

    def test_api_accepts_valid_image_and_returns_fields_and_rule_results(self):
        self.api.force_authenticate(self.user)

        def fake_process(scan):
            scan.extracted_text = "Basmati Rice"
            scan.ocr_confidence = 91
            scan.score = 100
            scan.status = Scan.Status.COMPLIANT
            scan.save()
            ExtractedField.objects.create(scan=scan, field_name="generic_name", value="Basmati Rice", confidence=95)
            rule = Rule.objects.create(code="generic_name", name="Generic name", weight=1)
            RuleResult.objects.create(scan=scan, rule=rule, status=RuleResult.Status.PASS, found_value="Basmati Rice", message="Found")

        with patch("scanner.api.process_scan", side_effect=fake_process):
            response = self.api.post("/api/scan/", {"images": image_upload()}, format="multipart")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["fields"]["generic_name"]["value"], "Basmati Rice")
        self.assertEqual(response.data["rule_results"][0]["code"], "generic_name")
        self.assertEqual(ScanImage.objects.count(), 1)

    def test_browser_upload_rejects_invalid_and_oversized_files(self):
        self.client.force_login(self.user)
        url = reverse("scanner:upload")
        invalid = self.client.post(url, {"images": image_upload("label.gif", "GIF")})
        self.assertEqual(invalid.status_code, 400)
        self.assertContains(invalid, "JPEG, PNG, or WEBP", status_code=400)
        large = SimpleUploadedFile("large.png", b"x" * (5 * 1024 * 1024 + 1), content_type="image/png")
        oversized = self.client.post(url, {"images": large})
        self.assertEqual(oversized.status_code, 400)
        self.assertContains(oversized, "larger than 5 MB", status_code=400)
        self.assertEqual(Scan.objects.count(), 0)

    def test_browser_upload_explains_unreadable_image(self):
        self.client.force_login(self.user)
        broken = SimpleUploadedFile("broken.png", b"not really an image", content_type="image/png")
        response = self.client.post(reverse("scanner:upload"), {"front_image": broken})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "could not be read as an image", status_code=400)

    def test_front_only_upload_is_accepted_with_missing_back_guidance(self):
        self.client.force_login(self.user)
        with patch("scanner.views.process_scan"):
            response = self.client.post(reverse("scanner:upload"), {"front_image": image_upload("front.png")})
        self.assertEqual(response.status_code, 302)
        scan = Scan.objects.latest("pk")
        result = self.client.get(reverse("scanner:result", args=[scan.pk]))
        self.assertContains(result, "No back image was uploaded")

    def test_no_text_scan_shows_friendly_result_message(self):
        self.client.force_login(self.user)

        def empty_ocr(scan):
            scan.status = Scan.Status.NEEDS_REVIEW
            scan.ocr_confidence = 0
            scan.extracted_text = ""
            scan.save()

        with patch("scanner.views.process_scan", side_effect=empty_ocr):
            response = self.client.post(reverse("scanner:upload"), {"front_image": image_upload("front.png")})
        scan = Scan.objects.latest("pk")
        result = self.client.get(reverse("scanner:result", args=[scan.pk]))
        self.assertContains(result, "No readable text was recorded for this image")

    def test_processing_exception_returns_review_scan_instead_of_server_error(self):
        self.client.force_login(self.user)
        with patch("scanner.views.process_scan", side_effect=RuntimeError("OCR backend unavailable")):
            response = self.client.post(reverse("scanner:upload"), {"front_image": image_upload("front.png")})
        self.assertEqual(response.status_code, 302)
        scan = Scan.objects.latest("pk")
        self.assertEqual(scan.status, Scan.Status.NEEDS_REVIEW)
        self.assertFalse(scan.violations().exists())
        self.assertIsNone(scan.score)
        self.assertEqual(scan.ocr_confidence, 0)
        result = self.client.get(reverse("scanner:result", args=[scan.pk]))
        self.assertContains(result, "could not be completed")
        self.assertNotContains(result, "Traceback")

    def test_upload_page_exposes_all_processing_stages(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("scanner:upload"))
        for label in ("Uploading", "Reading text", "Checking rules", "Completed"):
            self.assertContains(response, label)

    def test_browser_upload_accepts_valid_image(self):
        self.client.force_login(self.user)
        with patch("scanner.views.process_scan"):
            response = self.client.post(reverse("scanner:upload"), {"images": image_upload()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Scan.objects.count(), 1)
        self.assertEqual(ScanImage.objects.count(), 1)

    def test_landing_page_is_public_and_logged_in_users_go_to_dashboard(self):
        public_response = self.client.get(reverse("scanner:home"))
        self.assertEqual(public_response.status_code, 200)
        self.assertContains(public_response, "How it works")
        self.client.force_login(self.user)
        response = self.client.get(reverse("scanner:home"))
        self.assertRedirects(response, reverse("scanner:dashboard"))
