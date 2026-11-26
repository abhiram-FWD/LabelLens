from types import SimpleNamespace

from django.test import SimpleTestCase

from scanner.models import RuleResult, Scan
from scanner.services.assessment import assessment_from_results


class SharedAssessmentTests(SimpleTestCase):
    def test_provisional_score_excludes_review_and_not_applicable(self):
        assessment = assessment_from_results([
            SimpleNamespace(status=RuleResult.Status.PASS),
            SimpleNamespace(status=RuleResult.Status.FAIL),
            SimpleNamespace(status=RuleResult.Status.NEEDS_REVIEW),
            SimpleNamespace(status=RuleResult.Status.NOT_APPLICABLE),
        ])
        self.assertEqual(assessment["score"], 50)
        self.assertEqual(assessment["assessed"], 2)
        self.assertEqual(assessment["applicable"], 3)
        self.assertEqual(assessment["score_label"], "Provisional: 50% of assessed rules passed")
        self.assertEqual(assessment["display"], "Provisional: 50% of assessed rules passed (2 of 3 assessed)")
        self.assertEqual(assessment["verdict"], Scan.Status.NEEDS_REVIEW)
        self.assertEqual(assessment["verdict_label"], "Needs review")
        self.assertEqual(assessment["details_label"], "Violation found, needs review")
        self.assertEqual(
            assessment["score_explanation"],
            "1/2 applicable assessed rules passed (50%); 1 rule needs review; 1 rule not applicable and excluded from the score.",
        )

    def test_all_pass_is_final_compliant(self):
        assessment = assessment_from_results([
            SimpleNamespace(status=RuleResult.Status.PASS),
            SimpleNamespace(status=RuleResult.Status.PASS),
            SimpleNamespace(status=RuleResult.Status.NOT_APPLICABLE),
        ])
        self.assertEqual(assessment["score"], 100)
        self.assertEqual(assessment["verdict"], Scan.Status.COMPLIANT)
        self.assertEqual(assessment["score_explanation"], "2/2 applicable assessed rules passed (100%); 0 rules need review; 1 rule not applicable and excluded from the score.")

    def test_provisional_one_hundred_percent_is_explicitly_incomplete(self):
        assessment = assessment_from_results([
            SimpleNamespace(status=RuleResult.Status.PASS) for _ in range(6)
        ] + [SimpleNamespace(status=RuleResult.Status.NEEDS_REVIEW)])
        self.assertEqual(assessment["score"], 100)
        self.assertIn("Provisional", assessment["display"])
        self.assertIn("6 of 7 assessed", assessment["display"])
        self.assertEqual(assessment["needs_review"], 1)

    def test_zero_assessed_rules_never_reports_one_hundred_percent(self):
        assessment = assessment_from_results([
            SimpleNamespace(status=RuleResult.Status.NOT_APPLICABLE),
        ])
        self.assertIsNone(assessment["score"])
        self.assertEqual(assessment["verdict"], Scan.Status.NEEDS_REVIEW)
        self.assertEqual(assessment["score_label"], "Not assessed")

    def test_only_verified_failures_count_and_noncompliant_requires_complete_assessment(self):
        assessment = assessment_from_results([
            SimpleNamespace(status=RuleResult.Status.PASS),
            SimpleNamespace(status=RuleResult.Status.FAIL, source_type=RuleResult.SourceType.SYSTEM),
        ])
        self.assertEqual(assessment["failed"], 0)
        self.assertEqual(assessment["needs_review"], 1)
        self.assertEqual(assessment["verdict"], Scan.Status.NEEDS_REVIEW)

        complete = assessment_from_results([
            SimpleNamespace(status=RuleResult.Status.PASS),
            SimpleNamespace(status=RuleResult.Status.FAIL),
        ])
        self.assertEqual(complete["verdict"], Scan.Status.NON_COMPLIANT)
        self.assertEqual(complete["verdict_label"], "Non-compliant")
        self.assertEqual(complete["details_label"], "Violation found")
