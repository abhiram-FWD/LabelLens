from io import BytesIO, StringIO
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models.deletion import CASCADE, PROTECT, SET_NULL
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings
from PIL import Image

from scanner.models import ExtractedField, Product, Report, Rule, RuleResult, Scan, ScanImage


class SchemaIntegrityTests(TestCase):
    def setUp(self):
        self.media_dir = TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(self.media_dir.cleanup)
        self.user = get_user_model().objects.create_user(username="schema-user")

    def test_relationship_delete_policies_match_the_schema_contract(self):
        self.assertIs(Scan._meta.get_field("user").remote_field.on_delete, CASCADE)
        self.assertIs(Scan._meta.get_field("product").remote_field.on_delete, SET_NULL)
        for model in (ScanImage, ExtractedField, RuleResult):
            self.assertIs(model._meta.get_field("scan").remote_field.on_delete, CASCADE)
        self.assertIs(Report._meta.get_field("scan").remote_field.on_delete, CASCADE)
        self.assertIs(RuleResult._meta.get_field("rule").remote_field.on_delete, PROTECT)

    def test_scan_delete_cascades_rows_and_removes_image_and_report_files(self):
        scan = Scan.objects.create(user=self.user)
        image_buffer = BytesIO()
        Image.new("RGB", (16, 16), "white").save(image_buffer, format="PNG")
        image = ScanImage.objects.create(
            scan=scan,
            image=SimpleUploadedFile("label.png", image_buffer.getvalue(), content_type="image/png"),
        )
        report = Report.objects.create(
            scan=scan,
            pdf_file=SimpleUploadedFile("report.pdf", b"%PDF sample", content_type="application/pdf"),
        )
        rule = Rule.objects.create(code="delete-check", name="Delete check")
        ExtractedField.objects.create(scan=scan, field_name="name", value="Biscuit")
        RuleResult.objects.create(scan=scan, rule=rule, status=RuleResult.Status.PASS)
        image_name, report_name = image.image.name, report.pdf_file.name
        self.assertTrue(image.image.storage.exists(image_name))
        self.assertTrue(report.pdf_file.storage.exists(report_name))

        scan.delete()

        self.assertFalse(ScanImage.objects.filter(pk=image.pk).exists())
        self.assertFalse(ExtractedField.objects.filter(scan_id=scan.pk).exists())
        self.assertFalse(RuleResult.objects.filter(scan_id=scan.pk).exists())
        self.assertFalse(Report.objects.filter(pk=report.pk).exists())
        self.assertFalse(image.image.storage.exists(image_name))
        self.assertFalse(report.pdf_file.storage.exists(report_name))

    def test_product_deletion_nulls_scan_reference_and_rule_deletion_is_protected(self):
        product = Product.objects.create(name="Biscuit")
        scan = Scan.objects.create(user=self.user, product=product)
        product.delete()
        scan.refresh_from_db()
        self.assertIsNone(scan.product_id)

        rule = Rule.objects.create(code="protected-rule", name="Protected")
        RuleResult.objects.create(scan=scan, rule=rule, status=RuleResult.Status.FAIL)
        with self.assertRaises(ProtectedError):
            rule.delete()

    def test_database_rejects_out_of_range_scores_and_confidences(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Scan.objects.create(user=self.user, score=100.1)
        scan = Scan.objects.create(user=self.user)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Scan.objects.filter(pk=scan.pk).update(ocr_confidence=-0.1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ExtractedField.objects.create(scan=scan, field_name="bad", value="x", confidence=101)

    def test_unique_relationship_constraints_and_scan_summary_and_violations(self):
        scan = Scan.objects.create(
            user=self.user, score=60, status=Scan.Status.PARTIAL,
        )
        pass_rule = Rule.objects.create(code="summary-pass", name="Passed")
        fail_rule = Rule.objects.create(code="summary-fail", name="Failed")
        review_rule = Rule.objects.create(code="summary-review", name="Needs review")
        passed = RuleResult.objects.create(scan=scan, rule=pass_rule, status=RuleResult.Status.PASS)
        failed = RuleResult.objects.create(scan=scan, rule=fail_rule, status=RuleResult.Status.FAIL)
        RuleResult.objects.create(scan=scan, rule=review_rule, status=RuleResult.Status.NEEDS_REVIEW)
        ExtractedField.objects.create(scan=scan, field_name="generic_name", value="Biscuit")

        self.assertEqual(scan.scan_summary, {"passed": 1, "failed": 1, "needs_review": 1})
        self.assertEqual(list(scan.violations()), [failed])
        self.assertEqual(list(Scan.objects.filter(pk=scan.pk).violations()), [failed])
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RuleResult.objects.create(scan=scan, rule=pass_rule, status=RuleResult.Status.FAIL)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ExtractedField.objects.create(scan=scan, field_name="generic_name", value="Other")

        review_scan = Scan.objects.create(user=self.user, status=Scan.Status.NEEDS_REVIEW)
        review_fail = RuleResult.objects.create(scan=review_scan, rule=Rule.objects.create(code="review-fail", name="Review fail"), status=RuleResult.Status.FAIL)
        self.assertEqual(list(review_scan.violations()), [review_fail])

    def test_integrity_command_reports_a_clean_database(self):
        output = StringIO()
        call_command("check_integrity", stdout=output)
        self.assertIn("Integrity check passed", output.getvalue())
