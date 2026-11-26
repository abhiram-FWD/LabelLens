from django.contrib.auth import get_user_model
from django.test import TestCase

from scanner.models import ExtractedField, Product, Rule, RuleResult, Scan, ScanImage
from scanner.services.rules import VALIDATORS, evaluate


class EvaluateRulesTests(TestCase):
    scenarios = {
        "manufacturer_address": {
            "pass": ({"manufacturer": "Acme Foods, Pune 411001"}, ""),
            "review": ({"manufacturer": "Acme Foods, Pune"}, ""),
            "absent": ({}, ""),
        },
        "generic_name": {
            "pass": ({"generic_name": "Basmati Rice"}, ""),
            "review": ({"generic_name": "unknown"}, ""),
            "absent": ({}, ""),
        },
        "net_quantity": {
            "pass": ({"net_quantity": "500 g"}, ""),
            "review": ({"net_quantity": "500 oz"}, ""),
            "absent": ({}, ""),
        },
        "mfg_date": {
            "pass": ({"mfg_date": "04/2025"}, "Mfg: 04/2025"),
            "review": ({"mfg_date": "13/2025"}, ""),
            "absent": ({}, ""),
        },
        "mrp": {
            "pass": ({"mrp": "100"}, "MRP Rs 100 inclusive of all taxes"),
            "review": ({"mrp": "100"}, "MRP Rs 100"),
            "absent": ({}, ""),
        },
        "consumer_care": {
            "pass": ({"consumer_care": "9876543210 care@example.com"}, ""),
            "review": ({"consumer_care": "Customer care desk"}, ""),
            "absent": ({}, ""),
        },
        "country_of_origin": {
            "pass": ({"country_of_origin": "India"}, "Imported by: Example Imports"),
            "review": ({"country_of_origin": "12345"}, "Imported by: Example Imports"),
            "absent": ({}, "Imported by: Example Imports"),
        },
    }

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="rules-user", password="test-pass-123")
        for code in self.scenarios:
            Rule.objects.create(code=code, name=code.replace("_", " ").title(), weight=1)

    def _evaluate_case(self, fields, source_text=""):
        readable_text = (
            source_text + " readable package label showing the full back panel, all printed declarations, "
            "ingredient list, contact details, product instructions, and clearly visible lower and side sections."
        ).strip()
        scan = Scan.objects.create(
            user=self.user, extracted_text=readable_text, ocr_confidence=90,
        )
        for name, value in fields.items():
            evidence = source_text if name in {"mfg_date", "expiry_date"} else ""
            if name == "net_quantity":
                evidence = f"NET QUANTITY: {value}"
            elif name == "mrp":
                evidence = f"MRP Rs {value} inclusive of all taxes"
            ExtractedField.objects.create(
                scan=scan, field_name=name, value=value,
                original_value=value if evidence else "", original_evidence=evidence,
            )
        ScanImage.objects.create(
            scan=scan, side=ScanImage.Side.BACK, image="test-back.jpg",
            extracted_text=readable_text, ocr_confidence=90, ocr_source_pass="test_back_psm6",
        )
        evaluate(scan)
        return scan

    def test_every_registered_rule_has_a_validator(self):
        self.assertTrue(set(self.scenarios).issubset(VALIDATORS))

    def test_unitless_quantity_is_needs_review_and_keeps_ocr_pass_provenance(self):
        scan = Scan.objects.create(
            user=self.user, extracted_text="NET QUANTITY : 79.49", ocr_confidence=91,
        )
        field = ExtractedField.objects.create(
            scan=scan, field_name="net_quantity", value="79.49", confidence=72,
            original_value="79.49", original_confidence=72,
            original_evidence="NET QUANTITY : 79.49", source_pass="original_right_psm6",
            review_reason="The value is near the net quantity declaration, but its unit could not be read reliably.",
        )
        Rule.objects.get(code="net_quantity")
        evaluate(scan)
        result = scan.rule_results.get(rule__code="net_quantity")
        self.assertEqual(result.status, RuleResult.Status.NEEDS_REVIEW)
        self.assertEqual(result.source_pass, field.source_pass)
        self.assertIn("unit could not be read", result.message)

    def test_valid_date_without_matching_manufacturing_heading_needs_review(self):
        scan = Scan.objects.create(
            user=self.user, extracted_text="Use By: 10/SEP/2026", ocr_confidence=92,
        )
        ExtractedField.objects.create(
            scan=scan, field_name="mfg_date", value="10/SEP/2026", confidence=91,
            original_value="10/SEP/2026", original_confidence=91,
            original_evidence="Use By: 10/SEP/2026",
        )
        evaluate(scan)
        result = scan.rule_results.get(rule__code="mfg_date")
        self.assertEqual(result.status, RuleResult.Status.NEEDS_REVIEW)
        self.assertIn("does not confirm", result.message)

    def test_each_rule_passes_fails_or_is_missing_as_expected(self):
        for code, outcomes in self.scenarios.items():
            for expected_status, (fields, source_text) in outcomes.items():
                with self.subTest(rule=code, status=expected_status):
                    scan = self._evaluate_case(fields, source_text)
                    result = scan.rule_results.get(rule__code=code)
                    if expected_status == "review":
                        expected_status = RuleResult.Status.NEEDS_REVIEW
                    elif expected_status == "absent":
                        expected_status = RuleResult.Status.FAIL
                    self.assertEqual(result.status, expected_status)
                    self.assertTrue(result.message)

    def test_unweighted_score_and_verdict_use_assessed_rules(self):
        all_valid = {
            "manufacturer": "Acme Foods, Pune 411001",
            "generic_name": "Basmati Rice",
            "net_quantity": "500 g",
            "mfg_date": "04/2025",
            "mrp": "100",
            "consumer_care": "9876543210",
            "country_of_origin": "India",
        }
        compliant = self._evaluate_case(all_valid, "Pkd: 04/2025. MRP Rs 100 inclusive of all taxes")
        self.assertEqual(compliant.score, 100)
        self.assertEqual(compliant.status, Scan.Status.COMPLIANT)

        partial_fields = {key: value for key, value in all_valid.items() if key in {
            "manufacturer", "generic_name", "net_quantity"
        }}
        partial = self._evaluate_case(partial_fields)
        self.assertEqual(partial.score, 50)
        self.assertEqual(partial.status, Scan.Status.NON_COMPLIANT)

        non_compliant = self._evaluate_case({})
        self.assertEqual(non_compliant.score, 0)
        self.assertEqual(non_compliant.status, Scan.Status.NON_COMPLIANT)

    def test_evaluate_updates_existing_results_instead_of_duplicating(self):
        scan = self._evaluate_case({"generic_name": "Basmati Rice"})
        evaluate(scan)
        self.assertEqual(scan.rule_results.count(), len(self.scenarios))
        self.assertEqual(scan.rule_results.filter(rule__code="generic_name").count(), 1)

    def test_low_confidence_scan_needs_review_and_has_no_score(self):
        scan = Scan.objects.create(
            user=self.user,
            extracted_text="one two three four five six seven",
            ocr_confidence=22,
        )
        evaluate(scan)
        scan.refresh_from_db()
        self.assertEqual(scan.status, Scan.Status.NEEDS_REVIEW)
        self.assertIsNone(scan.score)
        self.assertEqual(
            set(scan.rule_results.values_list("status", flat=True)),
            {RuleResult.Status.NEEDS_REVIEW},
        )

    def test_confident_short_text_can_be_assessed_field_by_field(self):
        scan = Scan.objects.create(user=self.user, extracted_text="Rice 500 g", ocr_confidence=95)
        evaluate(scan)
        scan.refresh_from_db()
        self.assertEqual(scan.status, Scan.Status.NEEDS_REVIEW)
        self.assertIsNone(scan.score)

    def test_manually_verified_scan_runs_rules_despite_low_ocr_confidence(self):
        scan = Scan.objects.create(
            user=self.user, extracted_text="Rice 500 g", ocr_confidence=10,
            manually_verified=True,
        )
        ExtractedField.objects.create(scan=scan, field_name="generic_name", value="Rice")
        evaluate(scan)
        scan.refresh_from_db()
        self.assertNotEqual(scan.status, Scan.Status.NEEDS_REVIEW)
        self.assertIsNotNone(scan.score)

    def test_imported_and_domestic_products_apply_origin_rule_correctly(self):
        imported = self._evaluate_case(
            {"country_of_origin": "Thailand", "generic_name": "Rice"},
            "Imported by Example Imports, Bangkok Thailand",
        )
        self.assertEqual(
            imported.rule_results.get(rule__code="country_of_origin").status,
            RuleResult.Status.PASS,
        )
        domestic = self._evaluate_case(
            {"country_of_origin": "India", "generic_name": "Rice"},
            "Manufactured in Pune, India",
        )
        self.assertEqual(
            domestic.rule_results.get(rule__code="country_of_origin").status,
            RuleResult.Status.NOT_APPLICABLE,
        )

    def test_score_and_checked_count_ignore_non_applicable_rules(self):
        scan = self._evaluate_case(
            {"manufacturer": "Acme Foods Pune 411001", "generic_name": "Rice",
             "net_quantity": "1 kg", "mfg_date": "04/2025", "mrp": "100",
             "consumer_care": "9876543210", "country_of_origin": "India"},
            "Pkd: 04/2025. Manufactured in Pune, India. MRP inclusive of all taxes",
        )
        self.assertEqual(scan.score, 100)
        self.assertEqual(scan.rule_results.filter(status__in=(RuleResult.Status.PASS, RuleResult.Status.FAIL)).count(), 6)
        self.assertEqual(scan.rule_results.filter(status=RuleResult.Status.NOT_APPLICABLE).count(), 1)

    def test_score_excludes_review_and_not_applicable_but_counts_verified_results(self):
        fields = {
            "manufacturer": "Acme Foods Pune 411001", "generic_name": "Rice",
            "net_quantity": "1 kg", "mfg_date": "04/2025", "mrp": "100",
        }
        source = "Readable package label. MRP Rs 100 inclusive of all taxes. Customer care desk."
        care_rule = Rule.objects.get(code="consumer_care")
        care_rule.fix_suggestion = "Add readable contact details."
        care_rule.save(update_fields=("fix_suggestion",))
        review_scan = self._evaluate_case(fields, source)
        care_result = review_scan.rule_results.get(rule__code="consumer_care")
        self.assertEqual(care_result.status, RuleResult.Status.NEEDS_REVIEW)
        self.assertEqual(care_result.suggested_fix, "Add readable contact details.")
        self.assertEqual(review_scan.rule_results.get(rule__code="country_of_origin").status, RuleResult.Status.NOT_APPLICABLE)
        self.assertEqual(review_scan.score, 100)
        self.assertEqual(review_scan.status, Scan.Status.NEEDS_REVIEW)

        violation_fields = {key: value for key, value in fields.items() if key != "mfg_date"}
        date_rule = Rule.objects.get(code="mfg_date")
        date_rule.fix_suggestion = "Add the manufacture date."
        date_rule.save(update_fields=("fix_suggestion",))
        violation_scan = self._evaluate_case(violation_fields, source)
        date_result = violation_scan.rule_results.get(rule__code="mfg_date")
        self.assertEqual(date_result.status, RuleResult.Status.FAIL)
        self.assertEqual(date_result.suggested_fix, "Add the manufacture date.")
        self.assertEqual(violation_scan.rule_results.get(rule__code="consumer_care").status, RuleResult.Status.NEEDS_REVIEW)
        self.assertEqual(violation_scan.score, 80)
        self.assertEqual(violation_scan.status, Scan.Status.NON_COMPLIANT)

    def test_rule_results_keep_source_image_evidence_and_snapshot_fix_fields(self):
        rule = Rule.objects.get(code="mrp")
        rule.legal_reference = "Test reference"
        rule.fix_suggestion = "State the MRP clearly."
        rule.save(update_fields=("legal_reference", "fix_suggestion"))
        scan = self._evaluate_case(
            {"mrp": "100", "manufacturer": "Acme Foods Pune 411001", "generic_name": "Rice",
             "net_quantity": "1 kg", "mfg_date": "04/2025", "consumer_care": "9876543210"},
            "MRP Rs 100 inclusive of all taxes",
        )
        result = scan.rule_results.get(rule=rule)
        self.assertEqual(result.found_value, "100")
        self.assertEqual(result.source_image.side, ScanImage.Side.BACK)
        self.assertIn("MRP Rs 100", result.evidence)
        self.assertEqual(result.legal_reference, "Test reference")
        self.assertEqual(result.suggested_fix, "")

        review = scan.rule_results.get(rule__code="country_of_origin")
        self.assertEqual(review.status, RuleResult.Status.NOT_APPLICABLE)
        self.assertEqual(review.suggested_fix, "")

    def test_mrp_keyword_with_unreadable_amount_needs_review_not_fail(self):
        scan = self._evaluate_case({}, "MRP Rs ??? inclusive of all taxes. This package label is readable.")
        result = scan.rule_results.get(rule__code="mrp")
        self.assertEqual(result.status, RuleResult.Status.NEEDS_REVIEW)
        self.assertIn("could not be extracted reliably", result.message)

    def test_absent_manufacture_date_is_fail_only_with_readable_back(self):
        scan = self._evaluate_case({}, "Readable package label with product details and ingredients.")
        self.assertEqual(scan.rule_results.get(rule__code="mfg_date").status, RuleResult.Status.FAIL)

    def test_ocr_failure_does_not_claim_missing_declarations_as_verified_violations(self):
        scan = Scan.objects.create(user=self.user, extracted_text="", ocr_confidence=90)
        ScanImage.objects.create(
            scan=scan, side=ScanImage.Side.BACK, image="unreadable-back.jpg", extracted_text="",
        )
        evaluate(scan)
        for result in scan.rule_results.exclude(status=RuleResult.Status.NOT_APPLICABLE):
            self.assertEqual(result.status, RuleResult.Status.NEEDS_REVIEW, result.rule.code)

    def test_expiry_check_needs_review_when_ocr_cannot_read_the_back_label(self):
        rule = Rule.objects.create(code="expiry_date", name="Expiry date")
        scan = Scan.objects.create(user=self.user, extracted_text="", ocr_confidence=90)
        ScanImage.objects.create(scan=scan, side=ScanImage.Side.BACK, image="empty-back.jpg", extracted_text="")
        evaluate(scan)
        self.assertEqual(scan.rule_results.get(rule=rule).status, RuleResult.Status.NEEDS_REVIEW)

    def test_front_only_scan_does_not_fail_absent_back_declarations(self):
        scan = Scan.objects.create(
            user=self.user, extracted_text="Front label shows product name and net quantity clearly.",
            ocr_confidence=95,
        )
        ScanImage.objects.create(scan=scan, side=ScanImage.Side.FRONT, image="test-front.jpg")
        evaluate(scan)
        self.assertEqual(scan.rule_results.get(rule__code="mrp").status, RuleResult.Status.NEEDS_REVIEW)
        self.assertEqual(scan.status, Scan.Status.NEEDS_REVIEW)
        self.assertIsNone(scan.score)

    def test_expiry_rule_is_category_scoped_when_no_expiry_declaration_is_visible(self):
        rule = Rule.objects.create(
            code="expiry_date", name="Expiry date", applicable_categories="Dairy",
        )
        product = Product.objects.create(name="Soap", category="Personal care")
        scan = Scan.objects.create(
            user=self.user, product=product,
            extracted_text="Readable package label with enough words to assess all declarations.",
            ocr_confidence=95,
        )
        ScanImage.objects.create(scan=scan, side=ScanImage.Side.BACK, image="soap-back.jpg")
        evaluate(scan)
        self.assertEqual(scan.rule_results.get(rule=rule).status, RuleResult.Status.NOT_APPLICABLE)

    def test_valid_strict_patterns_pass_at_confidence_50_through_71(self):
        for confidence in (50, 60, 70, 71):
            with self.subTest(confidence=confidence):
                scan = Scan.objects.create(
                    user=self.user,
                    extracted_text="Pkd: 10/SEP/2026. MRP & 10.00 inclusive of all taxes; readable package declarations",
                    ocr_confidence=confidence,
                )
                ScanImage.objects.create(scan=scan, side=ScanImage.Side.BACK, image="strict-back.jpg")
                fields = {
                    "manufacturer": "Acme Foods, 12 Road, Delhi 110001",
                    "generic_name": "Basmati Rice", "net_quantity": "79.4 g",
                    "mfg_date": "10/SEP/2026", "mrp": "10.00",
                    "phone": "9876543210",
                }
                for name, value in fields.items():
                    evidence = ""
                    if name == "net_quantity":
                        evidence = f"NET QUANTITY: {value}"
                    elif name == "mrp":
                        evidence = f"MRP: {value} inclusive of all taxes"
                    elif name == "mfg_date":
                        evidence = "Pkd: 10/SEP/2026"
                    ExtractedField.objects.create(
                        scan=scan, field_name=name, value=value, confidence=confidence,
                        original_value=value if evidence else "", original_evidence=evidence,
                    )
                evaluate(scan)
                statuses = dict(scan.rule_results.values_list("rule__code", "status"))
                for code in ("manufacturer_address", "net_quantity", "mfg_date", "mrp", "consumer_care"):
                    self.assertEqual(statuses[code], RuleResult.Status.PASS)

    def test_confidence_49_requires_review_for_strict_fields(self):
        scan = Scan.objects.create(user=self.user, extracted_text="MRP 10.00", ocr_confidence=90)
        ScanImage.objects.create(scan=scan, side=ScanImage.Side.BACK, image="strict-back.jpg")
        for name, value in {
            "manufacturer": "Acme Foods, Delhi 110001", "net_quantity": "500 g",
            "mfg_date": "04/2025", "mrp": "10.00", "phone": "9876543210",
        }.items():
            ExtractedField.objects.create(scan=scan, field_name=name, value=value, confidence=49)
        evaluate(scan)
        for code in ("manufacturer_address", "net_quantity", "mfg_date", "mrp", "consumer_care"):
            self.assertEqual(scan.rule_results.get(rule__code=code).status, RuleResult.Status.NEEDS_REVIEW)

    def test_marketer_only_does_not_satisfy_responsible_party_rule(self):
        scan = self._evaluate_case({"marketer": "Sunrise Retail, Delhi 110001"}, "Marketed by: Sunrise Retail, Delhi 110001")
        result = scan.rule_results.get(rule__code="manufacturer_address")
        self.assertEqual(result.status, RuleResult.Status.FAIL)
        self.assertNotIn("Sunrise", result.found_value)

    def test_responsible_party_without_valid_pin_needs_review(self):
        scan = self._evaluate_case(
            {"manufacturer": "Acme Foods, Delhi 011001"},
            "Manufactured by: Acme Foods, Delhi 011001",
        )
        self.assertEqual(
            scan.rule_results.get(rule__code="manufacturer_address").status,
            RuleResult.Status.NEEDS_REVIEW,
        )

    def test_invalid_date_and_price_with_high_confidence_are_verified_failures(self):
        scan = Scan.objects.create(
            user=self.user, extracted_text="MFG 32/13/2026\nMRP Rs 0 inclusive of all taxes",
            ocr_confidence=95,
        )
        ScanImage.objects.create(scan=scan, side=ScanImage.Side.BACK, image="invalid-back.jpg")
        ExtractedField.objects.create(
            scan=scan, field_name="mfg_date", value="32/13/2026", confidence=90,
            original_value="32/13/2026", original_evidence="MFG 32/13/2026",
        )
        ExtractedField.objects.create(scan=scan, field_name="mrp", value="0", confidence=90)
        evaluate(scan)
        self.assertEqual(scan.rule_results.get(rule__code="mfg_date").status, RuleResult.Status.FAIL)
        self.assertEqual(scan.rule_results.get(rule__code="mrp").status, RuleResult.Status.FAIL)
