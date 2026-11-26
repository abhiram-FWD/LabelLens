"""Report broken references, invalid ranges, and missing stored scan files."""

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import Count

from scanner.models import ExtractedField, Report, Rule, RuleResult, Scan, ScanImage


class Command(BaseCommand):
    help = "Check scan relationships, data ranges, uniqueness, and stored files."

    def add_arguments(self, parser):
        parser.add_argument("--fail-on-issues", action="store_true")

    def handle(self, *args, **options):
        issues = []
        relation_checks = (
            (Scan, "user_id", Scan._meta.get_field("user").remote_field.model),
            (Scan, "product_id", Scan._meta.get_field("product").remote_field.model),
            (ScanImage, "scan_id", Scan),
            (ExtractedField, "scan_id", Scan),
            (RuleResult, "scan_id", Scan),
            (RuleResult, "rule_id", Rule),
            (Report, "scan_id", Scan),
        )
        quote = connection.ops.quote_name
        with connection.cursor() as cursor:
            for child, child_fk, parent in relation_checks:
                child_table = quote(child._meta.db_table)
                parent_table = quote(parent._meta.db_table)
                fk_column = quote(child_fk)
                pk_column = quote(parent._meta.pk.column)
                cursor.execute(
                    f"SELECT COUNT(*) FROM {child_table} c "
                    f"LEFT JOIN {parent_table} p ON c.{fk_column} = p.{pk_column} "
                    f"WHERE c.{fk_column} IS NOT NULL AND p.{pk_column} IS NULL"
                )
                count = cursor.fetchone()[0]
                if count:
                    issues.append(f"{child.__name__} has {count} orphan {child_fk} reference(s).")

        for scan in Scan.objects.all().only("pk", "score", "ocr_confidence", "status"):
            if scan.score is not None and not 0 <= scan.score <= 100:
                issues.append(f"Scan {scan.pk} has out-of-range score {scan.score}.")
            if scan.ocr_confidence is not None and not 0 <= scan.ocr_confidence <= 100:
                issues.append(f"Scan {scan.pk} has out-of-range OCR confidence {scan.ocr_confidence}.")
            if scan.status == Scan.Status.NEEDS_REVIEW and scan.score is not None:
                issues.append(f"Needs-review scan {scan.pk} has a compliance score.")
        for item in ExtractedField.objects.exclude(confidence__isnull=True).only("pk", "scan_id", "confidence"):
            if not 0 <= item.confidence <= 100:
                issues.append(f"Extracted field {item.pk} has out-of-range confidence {item.confidence}.")

        duplicate_checks = (
            (RuleResult, ("scan_id", "rule_id")),
            (ExtractedField, ("scan_id", "field_name")),
        )
        for model, fields in duplicate_checks:
            duplicates = model.objects.values(*fields).order_by().annotate(total=Count("pk")).filter(total__gt=1).count()
            if duplicates:
                issues.append(f"{model.__name__} has {duplicates} duplicate {', '.join(fields)} group(s).")

        for image in ScanImage.objects.all().only("pk", "image"):
            if image.image and not image.image.storage.exists(image.image.name):
                issues.append(f"Scan image record {image.pk} points to a missing file.")
        for report in Report.objects.all().only("pk", "pdf_file"):
            if report.pdf_file and not report.pdf_file.storage.exists(report.pdf_file.name):
                issues.append(f"Report record {report.pk} points to a missing file.")

        if issues:
            for issue in issues:
                self.stderr.write(self.style.ERROR(issue))
            self.stdout.write(self.style.ERROR(f"Integrity check found {len(issues)} issue(s)."))
            if options["fail_on_issues"]:
                from django.core.management.base import CommandError
                raise CommandError("Integrity issues found.")
        else:
            self.stdout.write(self.style.SUCCESS("Integrity check passed; no orphaned references or inconsistencies found."))
