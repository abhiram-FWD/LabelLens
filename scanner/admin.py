from django.contrib import admin

from .models import ExtractedField, Product, Report, Rule, RuleResult, Scan, ScanImage


class ScanImageInline(admin.TabularInline):
    model = ScanImage
    extra = 0


class RuleResultInline(admin.TabularInline):
    model = RuleResult
    extra = 0


class ExtractedFieldInline(admin.TabularInline):
    model = ExtractedField
    extra = 0


@admin.register(Scan)
class ScanAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "product", "status", "score", "ocr_confidence", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("product__name", "product__brand", "user__username", "extracted_text")
    inlines = (ScanImageInline, ExtractedFieldInline, RuleResultInline)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "brand", "category", "barcode", "created_at")
    list_filter = ("category", "created_at")
    search_fields = ("name", "brand", "barcode")


@admin.register(ScanImage)
class ScanImageAdmin(admin.ModelAdmin):
    list_display = ("id", "scan", "side", "uploaded_at")
    list_filter = ("side", "uploaded_at")
    search_fields = ("scan__product__name", "scan__user__username")


@admin.register(ExtractedField)
class ExtractedFieldAdmin(admin.ModelAdmin):
    list_display = ("field_name", "value", "confidence", "scan")
    list_filter = ("field_name",)
    search_fields = ("field_name", "value", "scan__product__name")


@admin.register(Rule)
class RuleAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "weight", "is_active", "legal_reference", "legal_note")
    list_filter = ("is_active", "weight")
    search_fields = ("code", "name", "legal_reference", "legal_note", "description")


@admin.register(RuleResult)
class RuleResultAdmin(admin.ModelAdmin):
    list_display = ("rule", "scan", "status", "found_value")
    list_filter = ("status", "rule")
    search_fields = ("rule__code", "rule__name", "scan__product__name", "found_value")


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ("scan", "pdf_file", "generated_at")
    list_filter = ("generated_at",)
    search_fields = ("scan__product__name", "scan__user__username")
