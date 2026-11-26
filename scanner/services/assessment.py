"""Shared rule scoring and scan verdict calculation."""

from scanner.models import RuleResult, Scan


def assessment_from_results(results):
    results = list(results)
    passed = sum(item.status == RuleResult.Status.PASS for item in results)
    # A system-generated failure has not been verified by OCR evidence or a
    # manual decision, so keep it in the review bucket.
    unverified_failures = sum(
        item.status == RuleResult.Status.FAIL
        and getattr(item, "source_type", RuleResult.SourceType.OCR) == RuleResult.SourceType.SYSTEM
        for item in results
    )
    failed = sum(
        item.status == RuleResult.Status.FAIL
        and getattr(item, "source_type", RuleResult.SourceType.OCR) != RuleResult.SourceType.SYSTEM
        for item in results
    )
    needs_review = sum(item.status == RuleResult.Status.NEEDS_REVIEW for item in results) + unverified_failures
    not_applicable = sum(item.status == RuleResult.Status.NOT_APPLICABLE for item in results)
    total_rules = len(results)
    assessed = passed + failed
    applicable = assessed + needs_review
    score = round(passed * 100 / assessed, 2) if assessed else None

    if needs_review or assessed == 0:
        verdict = Scan.Status.NEEDS_REVIEW
    elif failed:
        verdict = Scan.Status.NON_COMPLIANT
    else:
        verdict = Scan.Status.COMPLIANT

    if needs_review:
        score_label = f"Provisional: {score:.0f}% of assessed rules passed" if score is not None else "Provisional: not assessed"
    else:
        score_label = f"{score:.0f}%" if score is not None else "Not assessed"
    display = (
        f"{score_label} ({assessed} of {applicable} assessed)"
        if needs_review else score_label
    )
    review_count = f"{needs_review} rule{'s' if needs_review != 1 else ''} need{'s' if needs_review == 1 else ''} review"
    na_count = f"{not_applicable} rule{'s' if not_applicable != 1 else ''} not applicable"
    if assessed:
        score_explanation = (
            f"{passed}/{assessed} applicable assessed rules passed ({score:.0f}%); "
            f"{review_count}; {na_count} and excluded from the score."
        )
    else:
        score_explanation = (
            f"No applicable rules were fully assessed; {review_count}; {na_count}."
        )
    verdict_label = dict(Scan.Status.choices).get(verdict, "Needs review")
    if failed and needs_review:
        details_label = "Violation found, needs review"
    elif failed:
        details_label = "Violation found"
    else:
        details_label = verdict_label
    return {
        "passed": passed,
        "failed": failed,
        "needs_review": needs_review,
        "not_applicable": not_applicable,
        "total_rules": total_rules,
        "assessed": assessed,
        "applicable": applicable,
        "score": score,
        "score_label": score_label,
        "display": display,
        "score_explanation": score_explanation,
        "verdict": verdict,
        "verdict_label": verdict_label,
        "details_label": details_label,
        "ring_label": "Review" if needs_review else (f"{score:.0f}" if score is not None else "Not assessed"),
        "ring_subtitle": "provisional" if needs_review else ("score" if score is not None else ""),
    }


def assessment_for_scan(scan):
    return assessment_from_results(scan.rule_results.all())
