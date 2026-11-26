from django.conf import settings
from django.db import models
from django.db.models import Count, Q


class Product(models.Model):
    name = models.CharField(max_length=255)
    brand = models.CharField(max_length=255, blank=True)
    category = models.CharField(max_length=255, blank=True)
    barcode = models.CharField(max_length=64, null=True, blank=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("name", "id")

    def __str__(self):
        return f"{self.brand} {self.name}".strip()


class ScanQuerySet(models.QuerySet):
    def violations(self):
        """Return assessed, verified failing rule results for this scan queryset."""
        return RuleResult.objects.filter(
            scan__in=self,
            status=RuleResult.Status.FAIL,
        ).select_related("scan", "rule")


class Scan(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLIANT = "compliant", "Compliant"
        PARTIAL = "partial", "Partially compliant"
        NON_COMPLIANT = "non_compliant", "Non-compliant"
        NEEDS_REVIEW = "needs_review", "Needs review"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="scans")
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True, related_name="scans")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    score = models.FloatField(null=True, blank=True)
    ocr_confidence = models.FloatField(null=True, blank=True)
    manually_verified = models.BooleanField(default=False)
    extracted_text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ScanQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=("user", "created_at"), name="scan_user_created_idx"),
            models.Index(fields=("status",), name="scan_status_idx"),
            models.Index(fields=("user", "status"), name="scan_user_status_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(score__isnull=True) | Q(score__gte=0, score__lte=100),
                name="scan_score_0_100",
            ),
            models.CheckConstraint(
                condition=Q(ocr_confidence__isnull=True) | Q(ocr_confidence__gte=0, ocr_confidence__lte=100),
                name="scan_ocr_conf_0_100",
            ),
        ]

    def violations(self):
        """Return this scan's assessed, verified failing rule results."""
        return Scan.objects.filter(pk=self.pk).violations()

    @property
    def scan_summary(self):
        """Return live counts by passed, failed, and needs-review results."""
        counts = self.rule_results.aggregate(
            passed=Count("pk", filter=Q(status=RuleResult.Status.PASS)),
            failed=Count("pk", filter=Q(status=RuleResult.Status.FAIL)),
            needs_review=Count("pk", filter=Q(status=RuleResult.Status.NEEDS_REVIEW)),
        )
        return {key: value or 0 for key, value in counts.items()}

    def __str__(self):
        product = self.product.name if self.product_id else "Unidentified product"
        return f"Scan {self.pk} — {product}"


class ScanImage(models.Model):
    class Side(models.TextChoices):
        FRONT = "front", "Front"
        BACK = "back", "Back"
        OTHER = "other", "Other"

    scan = models.ForeignKey(Scan, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="scans/%Y/%m/")
    side = models.CharField(max_length=10, choices=Side.choices, default=Side.OTHER)
    extracted_text = models.TextField(blank=True)
    ocr_confidence = models.FloatField(null=True, blank=True)
    ocr_source_pass = models.CharField(max_length=80, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("uploaded_at", "id")
        indexes = [models.Index(fields=("scan", "side"), name="image_scan_side_idx")]

    def __str__(self):
        return f"{self.get_side_display()} image for scan {self.scan_id}"


class ExtractedField(models.Model):
    class SourceType(models.TextChoices):
        OCR = "ocr", "OCR"
        MANUAL = "manual", "Entered manually"

    scan = models.ForeignKey(Scan, on_delete=models.CASCADE, related_name="extracted_fields")
    field_name = models.CharField(max_length=100)
    value = models.TextField(blank=True)
    confidence = models.FloatField(null=True, blank=True)
    source_image = models.ForeignKey(ScanImage, on_delete=models.SET_NULL, null=True, blank=True, related_name="extracted_fields")
    source_type = models.CharField(max_length=10, choices=SourceType.choices, default=SourceType.OCR)
    original_value = models.TextField(blank=True)
    original_confidence = models.FloatField(null=True, blank=True)
    original_evidence = models.TextField(blank=True)
    original_evidence_coordinates = models.JSONField(default=list, blank=True)
    source_pass = models.CharField(max_length=80, blank=True)
    alternatives = models.JSONField(default=list, blank=True)
    review_reason = models.TextField(blank=True)

    class Meta:
        ordering = ("field_name",)
        constraints = [models.UniqueConstraint(fields=("scan", "field_name"), name="unique_scan_extracted_field")]
        indexes = [models.Index(fields=("field_name",), name="extract_field_name_idx")]
        constraints += [
            models.CheckConstraint(
                condition=Q(confidence__isnull=True) | Q(confidence__gte=0, confidence__lte=100),
                name="extract_conf_0_100",
            ),
        ]

    def __str__(self):
        return f"{self.field_name}: {self.value[:60]}"


class Rule(models.Model):
    code = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=255)
    legal_reference = models.CharField(max_length=255, blank=True)
    legal_note = models.TextField(blank=True, help_text="Short legal context shown with this check.")
    applicable_categories = models.CharField(
        max_length=500, blank=True,
        help_text="Comma-separated product categories. Leave blank to apply to all categories.",
    )
    description = models.TextField(blank=True)
    weight = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    fix_suggestion = models.TextField(blank=True)

    class Meta:
        ordering = ("code",)

    def __str__(self):
        return f"{self.code} — {self.name}"


class RuleResult(models.Model):
    class Status(models.TextChoices):
        PASS = "pass", "Pass"
        FAIL = "fail", "Fail"
        NOT_APPLICABLE = "not_applicable", "Not applicable"
        NEEDS_REVIEW = "needs_review", "Needs review"

    class SourceType(models.TextChoices):
        OCR = "ocr", "OCR"
        MANUAL = "manual", "Entered manually"
        SYSTEM = "system", "System"

    scan = models.ForeignKey(Scan, on_delete=models.CASCADE, related_name="rule_results")
    rule = models.ForeignKey(Rule, on_delete=models.PROTECT, related_name="results")
    source_image = models.ForeignKey(
        ScanImage, on_delete=models.SET_NULL, null=True, blank=True, related_name="rule_results",
    )
    status = models.CharField(max_length=20, choices=Status.choices)
    found_value = models.TextField(blank=True)
    evidence = models.TextField(blank=True)
    legal_reference = models.CharField(max_length=255, blank=True)
    suggested_fix = models.TextField(blank=True)
    message = models.TextField(blank=True)
    ocr_confidence = models.FloatField(null=True, blank=True)
    matched_line = models.TextField(blank=True)
    preceding_line = models.TextField(blank=True)
    following_line = models.TextField(blank=True)
    source_type = models.CharField(max_length=10, choices=SourceType.choices, default=SourceType.OCR)
    source_pass = models.CharField(max_length=80, blank=True)
    original_ocr_value = models.TextField(blank=True)
    original_ocr_evidence = models.TextField(blank=True)

    class Meta:
        ordering = ("rule__code",)
        constraints = [models.UniqueConstraint(fields=("scan", "rule"), name="unique_scan_rule_result")]
        indexes = [models.Index(fields=("scan", "status"), name="result_scan_status_idx")]

    def __str__(self):
        return f"{self.rule.code}: {self.get_status_display()}"


class Report(models.Model):
    scan = models.OneToOneField(Scan, on_delete=models.CASCADE, related_name="report")
    pdf_file = models.FileField(upload_to="reports/%Y/%m/")
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-generated_at",)

    def __str__(self):
        return f"Report for scan {self.scan_id}"
