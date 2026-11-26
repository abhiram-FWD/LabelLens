from io import BytesIO
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from unittest.mock import patch
from PIL import Image as PILImage
from config.urls import urlpatterns as root_urlpatterns

from scanner.models import ExtractedField, Product, Report, Rule, RuleResult, Scan, ScanImage
from scanner.services.report import generate_pdf


class ReportAndOwnershipTests(TestCase):
    def setUp(self):
        self.media_dir = TemporaryDirectory()
        self.media_settings = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.media_settings.enable()
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(username="scan-owner", password="owner-pass")
        self.other_user = user_model.objects.create_user(username="other-user", password="other-pass")
        product = Product.objects.create(name="Tea", brand="Example")
        self.owned_scan = Scan.objects.create(user=self.owner, product=product, status=Scan.Status.PARTIAL, score=70)
        self.other_scan = Scan.objects.create(user=self.other_user, product=product)

    def tearDown(self):
        self.media_settings.disable()
        self.media_dir.cleanup()

    def test_generate_pdf_returns_non_empty_pdf_bytes_and_saves_report(self):
        from PIL import Image as PILImage

        image_buffer = BytesIO()
        PILImage.new("RGB", (32, 24), "white").save(image_buffer, format="PNG")
        ScanImage.objects.create(
            scan=self.owned_scan,
            image=SimpleUploadedFile("label.png", image_buffer.getvalue(), content_type="image/png"),
        )
        pdf = generate_pdf(self.owned_scan)
        self.assertTrue(pdf)
        self.assertTrue(pdf.startswith(b"%PDF"))
        report = Report.objects.get(scan=self.owned_scan)
        self.assertTrue(report.pdf_file.name)
        with report.pdf_file.open("rb") as saved_file:
            self.assertEqual(saved_file.read(), pdf)

    def test_pdf_uses_persisted_fields_and_includes_review_evidence(self):
        ExtractedField.objects.create(
            scan=self.owned_scan, field_name="mfg_date", value="10/SEP/2026",
            confidence=87, original_value="10/SEP/2026", original_confidence=87,
            original_evidence="10/SEP/2026\\nPkd", review_reason="Verify date association.",
        )
        ExtractedField.objects.create(
            scan=self.owned_scan, field_name="expiry_date", value="09/MAR/2027",
            confidence=92, original_value="09/MAR/2027", original_confidence=92,
            original_evidence="09/MAR/2027\\nUse By",
        )
        rule = Rule.objects.create(code="fresh-pdf-state", name="Date", is_active=True)
        result = RuleResult.objects.create(
            scan=self.owned_scan, rule=rule, status=RuleResult.Status.NEEDS_REVIEW,
            message="Verify date.",
        )
        stale_scan = Scan.objects.prefetch_related(
            "extracted_fields", "rule_results__rule",
        ).get(pk=self.owned_scan.pk)
        ExtractedField.objects.filter(scan=self.owned_scan, field_name="mfg_date").update(
            value="10/SEP/2026 corrected", source_type=ExtractedField.SourceType.MANUAL,
            confidence=100,
        )
        RuleResult.objects.filter(pk=result.pk).update(status=RuleResult.Status.PASS)
        with patch("scanner.services.report._paragraph", wraps=__import__("scanner.services.report", fromlist=["_paragraph"])._paragraph) as paragraph:
            pdf = generate_pdf(stale_scan)
        self.assertTrue(pdf.startswith(b"%PDF"))
        rendered = " ".join(str(call.args[0]) for call in paragraph.call_args_list)
        self.assertIn("10/SEP/2026 corrected", rendered)
        self.assertIn("09/MAR/2027", rendered)
        self.assertIn("Verify date association.", rendered)
        self.assertIn("Entered manually", rendered)
        self.assertIn("100%", rendered)
        self.assertNotIn("Provisional", rendered)
        self.assertNotIn("No text was extracted", rendered)

    def test_pdf_generation_supports_compliant_non_compliant_and_review_scans(self):
        cases = (
            (Scan.Status.COMPLIANT, 100, RuleResult.Status.PASS),
            (Scan.Status.NON_COMPLIANT, 0, RuleResult.Status.FAIL),
            (Scan.Status.NEEDS_REVIEW, None, RuleResult.Status.NEEDS_REVIEW),
        )
        for index, (status, score, result_status) in enumerate(cases):
            with self.subTest(status=status):
                scan = Scan.objects.create(
                    user=self.owner, product=self.owned_scan.product,
                    status=status, score=score, ocr_confidence=22 if score is None else 92,
                    extracted_text="Example label has enough readable words here.",
                )
                rule = Rule.objects.create(
                    code=f"pdf-case-{index}", name="Net quantity",
                    legal_reference="Rule 6", fix_suggestion="State the quantity clearly.",
                )
                RuleResult.objects.create(
                    scan=scan, rule=rule, status=result_status,
                    found_value="500 g", message="Declaration result.",
                )
                pdf = generate_pdf(scan)
                self.assertTrue(pdf.startswith(b"%PDF"))
                self.assertTrue(Report.objects.filter(scan=scan, pdf_file__gt="").exists())

    def test_pdf_includes_each_image_grouped_fields_evidence_and_short_rule_reference(self):
        from PIL import Image as PILImage

        for side in (ScanImage.Side.FRONT, ScanImage.Side.BACK):
            buffer = BytesIO()
            PILImage.new("RGB", (24, 18), "white").save(buffer, format="PNG")
            ScanImage.objects.create(
                scan=self.owned_scan, side=side,
                image=SimpleUploadedFile(f"{side}.png", buffer.getvalue(), content_type="image/png"),
            )
        ExtractedField.objects.create(scan=self.owned_scan, field_name="mrp", value="10.00", confidence=32)
        rule = Rule.objects.create(code="pdf-reference", name="Maximum retail price")
        RuleResult.objects.create(
            scan=self.owned_scan, rule=rule, status=RuleResult.Status.NEEDS_REVIEW,
            legal_reference="Legal Metrology (Packaged Commodities) Rules, Rule 6(1)(b)",
            found_value="10.00", evidence="MRP %10.00", message="Verify the amount.",
        )
        from reportlab.platypus import Image as ReportImage, KeepTogether as ReportKeepTogether, Paragraph, Table
        from scanner.services import report as report_service
        original_build = report_service.SimpleDocTemplate.build
        captured = {}

        def capture_build(document, story, **kwargs):
            captured["story"] = list(story)
            return original_build(document, story, **kwargs)

        with patch.object(report_service.SimpleDocTemplate, "build", autospec=True, side_effect=capture_build):
            pdf = generate_pdf(self.owned_scan)
        story = captured["story"]
        def count_images(items):
            return sum(
                1 if isinstance(item, ReportImage) else
                count_images(item._content) if isinstance(item, ReportKeepTogether) else 0
                for item in items
            )
        self.assertEqual(count_images(story), 2)
        def collect_text(items):
            output = []
            for item in items:
                if isinstance(item, Paragraph):
                    output.append(item.getPlainText())
                elif isinstance(item, ReportKeepTogether):
                    output.extend(collect_text(item._content))
                elif isinstance(item, Table):
                    for row in item._cellvalues:
                        for cell in row:
                            if isinstance(cell, (Paragraph, Table)):
                                output.extend(collect_text([cell]))
                            elif isinstance(cell, (list, tuple)):
                                output.extend(collect_text(cell))
                            elif isinstance(cell, str):
                                output.append(cell)
            return output

        paragraph_text = " ".join(collect_text(story))
        self.assertIn("Pricing", paragraph_text)
        self.assertIn("Uncertain", paragraph_text)
        self.assertIn("Rule 6(1)(b)", paragraph_text)
        self.assertNotIn("Legal Metrology (Packaged Commodities) Rules", paragraph_text)
        self.assertIn("MRP %10.00", paragraph_text)
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_pdf_contains_needs_review_section_for_review_results(self):
        rule = Rule.objects.create(code="review-pdf", name="Date")
        RuleResult.objects.create(scan=self.owned_scan, rule=rule, status=RuleResult.Status.NEEDS_REVIEW)
        pass_rule = Rule.objects.create(code="review-pdf-pass", name="Product name")
        RuleResult.objects.create(scan=self.owned_scan, rule=pass_rule, status=RuleResult.Status.PASS)
        from reportlab.platypus import Paragraph
        from scanner.services import report as report_service
        original_build = report_service.SimpleDocTemplate.build
        captured = {}

        def capture_build(document, story, **kwargs):
            captured["story"] = list(story)
            return original_build(document, story, **kwargs)

        with patch.object(report_service.SimpleDocTemplate, "build", autospec=True, side_effect=capture_build):
            generate_pdf(self.owned_scan)
        from reportlab.platypus import Table
        def collect_text(items):
            output = []
            for item in items:
                if isinstance(item, Paragraph):
                    output.append(item.getPlainText())
                elif isinstance(item, Table):
                    for row in item._cellvalues:
                        for cell in row:
                            if isinstance(cell, (Paragraph, Table)):
                                output.extend(collect_text([cell]))
                            elif isinstance(cell, (list, tuple)):
                                output.extend(collect_text(cell))
            return output

        paragraph_text = " ".join(collect_text(captured["story"]))
        self.assertIn("Needs review", paragraph_text)
        self.assertIn("Provisional: 100% of assessed rules passed (1 of 2 assessed)", paragraph_text)

    def test_pdf_uses_display_labels_source_image_manual_label_and_review_reason(self):
        from PIL import Image as PILImage
        from reportlab.platypus import Image as ReportImage, KeepTogether as ReportKeepTogether, Paragraph, Table
        from scanner.services import report as report_service

        images = []
        for side in (ScanImage.Side.FRONT, ScanImage.Side.BACK):
            buffer = BytesIO()
            PILImage.new("RGB", (24, 18), "white").save(buffer, format="PNG")
            images.append(ScanImage.objects.create(
                scan=self.owned_scan, side=side,
                image=SimpleUploadedFile(f"{side}.png", buffer.getvalue(), content_type="image/png"),
                extracted_text=f"Text from {side} image",
            ))
        ExtractedField.objects.create(
            scan=self.owned_scan, field_name="mrp_inclusive_of_taxes", value="Yes", confidence=100,
            source_image=images[1], source_type=ExtractedField.SourceType.MANUAL,
            original_value="Y3s", original_confidence=34, original_evidence="MRP Y3s",
        )
        pass_rule = Rule.objects.create(code="pdf-display-pass", name="MRP declaration")
        review_rule = Rule.objects.create(code="pdf-display-review", name="Date declaration")
        fail_rule = Rule.objects.create(code="pdf-display-fail", name="Quantity declaration")
        na_rule = Rule.objects.create(code="pdf-display-na", name="Country declaration")
        RuleResult.objects.create(scan=self.owned_scan, rule=pass_rule, status=RuleResult.Status.PASS)
        RuleResult.objects.create(scan=self.owned_scan, rule=fail_rule, status=RuleResult.Status.FAIL)
        RuleResult.objects.create(
            scan=self.owned_scan, rule=review_rule, status=RuleResult.Status.NEEDS_REVIEW,
            source_image=images[1], source_type=RuleResult.SourceType.MANUAL, evidence="Entered manually",
            message="",
        )
        RuleResult.objects.create(scan=self.owned_scan, rule=na_rule, status=RuleResult.Status.NOT_APPLICABLE)
        original_build = report_service.SimpleDocTemplate.build
        captured = {}

        def capture_build(document, story, **kwargs):
            captured["story"] = list(story)
            return original_build(document, story, **kwargs)

        with patch.object(report_service.SimpleDocTemplate, "build", autospec=True, side_effect=capture_build):
            pdf = generate_pdf(self.owned_scan)

        def collect_text(items):
            output = []
            for item in items:
                if isinstance(item, Paragraph):
                    output.append(item.getPlainText())
                elif isinstance(item, ReportKeepTogether):
                    output.extend(collect_text(item._content))
                elif isinstance(item, Table):
                    for row in item._cellvalues:
                        for cell in row:
                            if isinstance(cell, (Paragraph, Table)):
                                output.extend(collect_text([cell]))
                            elif isinstance(cell, (list, tuple)):
                                output.extend(collect_text(cell))
                            elif isinstance(cell, str):
                                output.append(cell)
            return output

        text = " ".join(collect_text(captured["story"]))
        self.assertEqual(sum(
            1 if isinstance(item, ReportImage) else
            sum(isinstance(child, ReportImage) for child in item._content) if isinstance(item, ReportKeepTogether) else 0
            for item in captured["story"]
        ), 2)
        self.assertIn("MRP inclusive of taxes", text)
        self.assertIn("Source image: Back", text)
        self.assertIn("Entered manually", text)
        self.assertIn("could not be verified from the available label text", text)
        self.assertIn("N/A", text)
        self.assertIn("Violation found, needs review", text)
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_user_cannot_access_another_users_scan_result_report_or_actions(self):
        client = Client()
        client.login(username="scan-owner", password="owner-pass")
        scan_id = self.other_scan.pk

        self.assertEqual(client.get(reverse("scanner:result", args=[scan_id])).status_code, 404)
        self.assertEqual(client.get(reverse("scanner:download_report", args=[scan_id])).status_code, 404)
        self.assertEqual(client.post(reverse("scanner:rescan", args=[scan_id])).status_code, 404)
        self.assertEqual(client.post(reverse("scanner:delete_scan", args=[scan_id])).status_code, 404)
        self.assertTrue(Scan.objects.filter(pk=scan_id).exists())

    def test_dashboard_and_history_are_user_scoped_and_filterable(self):
        client = Client()
        client.login(username="scan-owner", password="owner-pass")
        dashboard = client.get(reverse("scanner:dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "statusChart")
        self.assertContains(dashboard, "failedRulesChart")
        self.assertContains(dashboard, "scansOverTimeChart")

        filtered = client.get(reverse("scanner:history"), {"status": "needs_review", "q": "Tea"})
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(list(filtered.context["scans"]), [self.owned_scan])
        self.assertNotContains(filtered, f"Scan #{self.other_scan.pk}")

    def test_dashboard_counts_rule_results_and_needs_review_scans(self):
        review_scan = Scan.objects.create(
            user=self.owner, status=Scan.Status.NEEDS_REVIEW, score=None,
            extracted_text="text too faint to read", ocr_confidence=12,
        )
        statuses = [
            RuleResult.Status.PASS,
            RuleResult.Status.FAIL,
            RuleResult.Status.NEEDS_REVIEW,
            RuleResult.Status.NEEDS_REVIEW,
        ]
        for index, status in enumerate(statuses):
            rule = Rule.objects.create(code=f"dashboard-count-{index}", name=f"Dashboard count {index}")
            RuleResult.objects.create(scan=review_scan, rule=rule, status=status)

        self.client.force_login(self.owner)
        response = self.client.get(reverse("scanner:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Violation found, needs review")
        self.assertEqual(response.context["rule_result_counts"], {
            "pass": 1, "fail": 1, "needs_review": 2,
        })
        self.assertEqual(response.context["failed_check_count"], 1)
        self.assertEqual(response.context["needs_review_rule_count"], 2)
        self.assertEqual(response.context["assessed_rule_count"], 2)
        self.assertEqual(response.context["compliance_rate"], 50)
        self.assertEqual(response.context["compliance_display"], "Provisional: 50% of assessed rules passed (2 of 4 assessed)")
        self.assertEqual(response.context["verified_violation_count"], 1)
        self.assertEqual(response.context["recent_scans"][0], review_scan)
        review_count = next(
            item["count"] for item in response.context["status_data"]
            if item["label"] == "Needs review"
        )
        self.assertEqual(review_count, 2)

    def test_result_page_groups_fields_and_hides_empty_rule_sections(self):
        image = ScanImage.objects.create(scan=self.owned_scan, side=ScanImage.Side.BACK, image="evidence.png")
        ExtractedField.objects.create(scan=self.owned_scan, field_name="generic_name", value="Tea", confidence=94)
        ExtractedField.objects.create(scan=self.owned_scan, field_name="manufacturer", value="Example Foods 411001", confidence=90)
        ExtractedField.objects.create(scan=self.owned_scan, field_name="net_quantity", value="500 g", confidence=92)
        rule = Rule.objects.create(code="result-evidence", name="Label details", legal_reference="Rule 6")
        RuleResult.objects.create(
            scan=self.owned_scan, rule=rule, status=RuleResult.Status.PASS,
            found_value="500 g", source_image=image, evidence="Net quantity: 500 g",
            legal_reference="Rule 6",
        )
        self.client.force_login(self.owner)
        response = self.client.get(reverse("scanner:result", args=[self.owned_scan.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Product Details")
        self.assertContains(response, "Manufacturer Details")
        self.assertContains(response, "Quantity")
        self.assertContains(response, "View evidence")
        self.assertContains(response, "Net quantity: 500 g")
        self.assertContains(response, "PASS")
        self.assertNotContains(response, "No checks in this section")
        self.assertNotContains(response, "Violations")
        self.assertNotContains(response, "Needs Review")
        self.assertNotContains(response, "Not Applicable")

    def test_verified_violations_match_on_result_dashboard_and_pdf(self):
        violation_rule = Rule.objects.create(code="verified-failure", name="Declared quantity")
        missing_rule = Rule.objects.create(code="unverified-missing", name="Care details")
        violation = RuleResult.objects.create(
            scan=self.owned_scan, rule=violation_rule, status=RuleResult.Status.FAIL,
        )
        RuleResult.objects.create(
            scan=self.owned_scan, rule=missing_rule, status=RuleResult.Status.NEEDS_REVIEW,
        )
        self.client.force_login(self.owner)

        result = self.client.get(reverse("scanner:result", args=[self.owned_scan.pk]))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(list(result.context["violations"]), [violation])
        self.assertEqual(result.context["assessment"]["verdict"], Scan.Status.NEEDS_REVIEW)
        self.assertEqual(result.context["assessment_verdict_label"], "Needs review")
        self.assertContains(result, "Violation found, needs review")
        self.assertEqual(result.context["assessment"]["needs_review"], 1)

        dashboard = self.client.get(reverse("scanner:dashboard"))
        self.assertEqual(dashboard.context["verified_violation_count"], 1)

        pdf = generate_pdf(self.owned_scan)
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_manual_review_form_saves_corrected_values_and_reassesses(self):
        scan = Scan.objects.create(
            user=self.owner, status=Scan.Status.NEEDS_REVIEW, score=None,
            extracted_text="Rice", ocr_confidence=10,
        )
        Rule.objects.create(code="generic_name", name="Generic name", weight=1)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("scanner:result", args=[scan.pk]))
        self.assertContains(response, "Text could not be read reliably")
        response = self.client.post(reverse("scanner:result", args=[scan.pk]), {
            "action": "manual_verify", "mrp": "20", "net_quantity": "500 g",
            "mfg_date": "04/2025", "manufacturer": "Example Foods 411001",
            "consumer_care": "9876543210", "country_of_origin": "India",
            "generic_name": "Biscuit",
        })
        self.assertEqual(response.status_code, 302)
        scan.refresh_from_db()
        self.assertTrue(scan.manually_verified)
        self.assertNotEqual(scan.status, Scan.Status.NEEDS_REVIEW, (scan.score, list(scan.rule_results.values_list("status", "message"))))
        self.assertEqual(scan.extracted_fields.get(field_name="generic_name").value, "Biscuit")
        self.assertTrue(scan.rule_results.exists())

    def test_single_uncertain_rule_does_not_show_global_ocr_warning(self):
        scan = Scan.objects.create(
            user=self.owner, extracted_text="Readable label text with enough words to assess.",
            ocr_confidence=90,
        )
        rule = Rule.objects.create(code="generic_name", name="Generic name")
        ExtractedField.objects.create(scan=scan, field_name="generic_name", value="Unclear", confidence=49)
        from scanner.services.rules import evaluate
        evaluate(scan)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("scanner:result", args=[scan.pk]))
        self.assertEqual(response.context["needs_review_count"], 1)
        self.assertFalse(response.context["global_ocr_warning"])
        self.assertNotContains(response, "Text could not be read reliably.")
        self.assertContains(response, "OCR confidence is below 50%")

    def test_recheck_form_edits_any_extracted_field_and_reassesses(self):
        scan = Scan.objects.create(
            user=self.owner, status=Scan.Status.PARTIAL, score=50,
            extracted_text="Readable package label declarations are printed here.",
            ocr_confidence=90,
        )
        rule = Rule.objects.create(code="generic_name", name="Generic name")
        RuleResult.objects.create(scan=scan, rule=rule, status=RuleResult.Status.FAIL)
        from scanner.models import ExtractedField
        ExtractedField.objects.create(scan=scan, field_name="generic_name", value="Unknown", confidence=55)
        self.client.force_login(self.owner)
        response = self.client.post(reverse("scanner:result", args=[scan.pk]), {
            "action": "recheck_fields", "field_generic_name": "Gluco Biscuits",
        })
        self.assertEqual(response.status_code, 302)
        scan.refresh_from_db()
        self.assertEqual(scan.extracted_fields.get(field_name="generic_name").value, "Gluco Biscuits")
        field = scan.extracted_fields.get(field_name="generic_name")
        result = scan.rule_results.get(rule=rule)
        self.assertEqual(result.status, RuleResult.Status.PASS)
        self.assertEqual(field.source_type, ExtractedField.SourceType.MANUAL)
        self.assertEqual(field.original_value, "Unknown")
        self.assertEqual(field.original_confidence, 55)
        self.assertEqual(result.source_type, RuleResult.SourceType.MANUAL)
        self.assertEqual(result.evidence, "Entered manually")
        self.assertEqual(result.original_ocr_value, "Unknown")

    def test_history_date_score_and_sort_filters(self):
        score_scan = Scan.objects.create(user=self.owner, product=self.owned_scan.product, score=92, status=Scan.Status.COMPLIANT)
        score_rule = Rule.objects.create(code="history-score-pass", name="History score")
        RuleResult.objects.create(scan=score_scan, rule=score_rule, status=RuleResult.Status.PASS)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("scanner:history"), {
            "date_from": self.owned_scan.created_at.date().isoformat(),
            "score_min": "90", "score_max": "100", "sort": "score_high",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual([scan.score for scan in response.context["scans"]], [92])

    def test_upload_slots_preserve_front_back_and_side_labels(self):
        def png(name):
            buffer = BytesIO()
            from PIL import Image as PILImage
            PILImage.new("RGB", (12, 12), "white").save(buffer, format="PNG")
            return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")

        self.client.force_login(self.owner)
        with patch("scanner.views.process_scan"):
            response = self.client.post(reverse("scanner:upload"), {
                "front_image": png("front.png"),
                "back_image": png("back.png"),
                "side_images": [png("side.png")],
            })
        self.assertEqual(response.status_code, 302)
        scan = Scan.objects.latest("pk")
        self.assertEqual(set(scan.images.values_list("side", flat=True)), {
            "front", "back", "other",
        })

    def test_profile_password_change_uses_django_auth_views(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("accounts:password_change"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Change password")
        response = self.client.post(reverse("accounts:password_change"), {
            "old_password": "owner-pass",
            "new_password1": "Safe-new-password-123!",
            "new_password2": "Safe-new-password-123!",
        })
        self.assertRedirects(response, reverse("accounts:password_change_done"))

    def test_unidentified_product_can_be_named_from_the_result_page(self):
        scan = Scan.objects.create(user=self.owner)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("scanner:result", args=[scan.pk]))
        self.assertContains(response, "Unidentified product")
        response = self.client.post(reverse("scanner:result", args=[scan.pk]), {
            "action": "edit_product", "product_name": "Gluco Biscuits", "brand": "Parle-G",
        })
        self.assertEqual(response.status_code, 302)
        scan.refresh_from_db()
        self.assertEqual(scan.product.name, "Gluco Biscuits")
        self.assertEqual(scan.product.brand, "Parle-G")

    def test_delete_scan_removes_only_the_current_users_scan(self):
        client = Client()
        client.login(username="scan-owner", password="owner-pass")
        response = client.post(reverse("scanner:delete_scan", args=[self.owned_scan.pk]))
        self.assertRedirects(response, reverse("scanner:history"))
        self.assertFalse(Scan.objects.filter(pk=self.owned_scan.pk).exists())
        self.assertTrue(Scan.objects.filter(pk=self.other_scan.pk).exists())


class ProductionMediaServingTests(TransactionTestCase):
    def test_uploaded_image_is_served_when_debug_is_false_only_to_authenticated_users(self):
        media_route = next(pattern for pattern in root_urlpatterns if getattr(pattern, "name", None) == "protected_media")
        route_media_root = media_route.default_args["document_root"]
        image_buffer = BytesIO()
        PILImage.new("RGB", (32, 24), "white").save(image_buffer, format="PNG")

        with override_settings(DEBUG=False, MEDIA_ROOT=route_media_root):
            owner = get_user_model().objects.create_user(username="production-media-owner", password="owner-pass")
            scan = Scan.objects.create(user=owner)
            image = ScanImage.objects.create(
                scan=scan,
                image=SimpleUploadedFile("production-media-test.png", image_buffer.getvalue(), content_type="image/png"),
            )
            response = None
            try:
                url = image.image.url
                self.assertEqual(self.client.get(url).status_code, 302)
                self.client.force_login(owner)
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "image/png")
                self.assertEqual(b"".join(response.streaming_content), image_buffer.getvalue())
            finally:
                if response is not None:
                    response.close()
                image.image.storage.delete(image.image.name)
