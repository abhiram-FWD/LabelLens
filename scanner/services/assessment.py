"""Shared rule scoring and scan verdict calculation."""

from scanner.models import RuleResult, Scan


def assessment_from_results(results):
    results = list(results)
    passed = sum(item.status == RuleResult.Status.PASS for item in results)
    failed = sum(item.status == RuleResult.Status.FAIL for item in results)
    needs_review = sum(item.status == RuleResult.Status.NEEDS_REVIEW for item in results)
    not_applicable = sum(item.status == RuleResult.Status.NOT_APPLICABLE for item in results)
    assessed = passed + failed
    applicable = assessed + needs_review
    score = round(passed * 100 / assessed, 2) if assessed else None

    if failed:
        verdict = Scan.Status.NON_COMPLIANT
    elif needs_review or assessed == 0:
        verdict = Scan.Status.NEEDS_REVIEW
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
    return {
        "passed": passed,
        "failed": failed,
        "needs_review": needs_review,
        "not_applicable": not_applicable,
        "assessed": assessed,
        "applicable": applicable,
        "score": score,
        "score_label": score_label,
        "display": display,
        "verdict": verdict,
    }


def assessment_for_scan(scan):
    return assessment_from_results(scan.rule_results.all())
