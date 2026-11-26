from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from PIL import Image as PILImage

from scanner.models import ExtractedField, Product, Rule, RuleResult, Scan, ScanImage
from scanner.services.field_labels import display_ocr_text, field_label


def uploaded_image(name):
    buffer = BytesIO()
    PILImage.new("RGB", (30, 20), "white").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


class PresentationTests(TestCase):
    def setUp(self):
        self.media_dir = TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.media_override.enable()
        user = get_user_model().objects.create_user(username="presentation", password="pass")
        self.client = Client()
        self.client.force_login(user)
        product = Product.objects.create(name="Example food")
        self.scan = Scan.objects.create(user=user, product=product, status=Scan.Status.NEEDS_REVIEW)

    def tearDown(self):
        self.media_override.disable()
        self.media_dir.cleanup()

    def test_field_labels_cover_known_and_unknown_names(self):
        self.assertEqual(field_label("Batch_Number"), "Batch number")
        self.assertEqual(field_label("Mrp_Inclusive_Of_Taxes"), "MRP inclusive of taxes")
        self.assertEqual(field_label("custom_unknown_value"), "Custom Unknown Value")

    def test_templates_use_html_middle_dot_entity_and_utf8_charset(self):
        template_root = Path(__file__).resolve().parents[2] / "templates"
        templates = list(template_root.rglob("*.html"))
        self.assertTrue(templates)
        for template in templates:
            self.assertNotIn("·", template.read_text(encoding="utf-8"), str(template))
        base = (template_root / "base.html").read_text(encoding="utf-8")
        self.assertIn('<meta charset="utf-8">', base)

    def test_raw_ocr_is_separated_filtered_and_keeps_original_text(self):
        original_front = "x\n₹\ng\nMRP\n\nA"
        original_back = "ab\nkg\nUse By\n09/MAR/2027"
        front = ScanImage.objects.create(scan=self.scan, side=ScanImage.Side.FRONT, image=uploaded_image("front.png"), extracted_text=original_front)
        ScanImage.objects.create(scan=self.scan, side=ScanImage.Side.BACK, image=uploaded_image("back.png"), extracted_text=original_back)

        response = self.client.get(reverse("scanner:result", args=[self.scan.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Front")
        self.assertContains(response, "Back")
        self.assertContains(response, "₹")
        self.assertContains(response, "09/MAR/2027")
        self.assertNotContains(response, ">x<")
        self.assertNotContains(response, ">A<")
        self.assertNotContains(response, ">ab<")
        self.assertNotContains(response, "open=")
        front.refresh_from_db()
        self.assertEqual(front.extracted_text, original_front)

    def test_single_image_has_no_empty_back_tab_and_known_label_is_used(self):
        ScanImage.objects.create(scan=self.scan, side=ScanImage.Side.FRONT, image=uploaded_image("front.png"), extracted_text="MRP 10.00")
        ExtractedField.objects.create(scan=self.scan, field_name="mrp_inclusive_of_taxes", value="Yes", confidence=94)
        response = self.client.get(reverse("scanner:result", args=[self.scan.pk]))
        self.assertContains(response, "MRP inclusive of taxes")
        self.assertNotContains(response, "Back</button>")
        self.assertNotContains(response, "Mrp_Inclusive_Of_Taxes")

    def test_short_line_filter_retains_declaration_markers_currency_and_units(self):
        self.assertEqual(display_ocr_text("\nx\n₹\ng\nkg\nMRP\n10\nA1\n"), "₹\ng\nkg\nMRP\n10\nA1")

    def test_dashboard_uses_shared_score_and_has_one_verified_violation_count(self):
        statuses = (RuleResult.Status.PASS, RuleResult.Status.FAIL, RuleResult.Status.NEEDS_REVIEW, RuleResult.Status.NOT_APPLICABLE)
        for index, status in enumerate(statuses):
            rule = Rule.objects.create(code=f"presentation-{index}", name=f"Rule {index}")
            RuleResult.objects.create(scan=self.scan, rule=rule, status=status, message="Check this declaration" if status == RuleResult.Status.NEEDS_REVIEW else "")

        dashboard = self.client.get(reverse("scanner:dashboard"))
        result = self.client.get(reverse("scanner:result", args=[self.scan.pk]))
        self.assertContains(dashboard, "Provisional: 50% of assessed rules passed (2 of 3 assessed)")
        self.assertNotContains(dashboard, ">Failed</span>")
        self.assertContains(dashboard, "Violations")
        self.assertEqual(dashboard.context["rule_result_counts"][RuleResult.Status.NOT_APPLICABLE], 1)
        self.assertEqual(dashboard.context["compliance_display"], result.context["assessment"]["display"])
