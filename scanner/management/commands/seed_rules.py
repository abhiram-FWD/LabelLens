from django.core.management.base import BaseCommand

from scanner.models import Rule


RULES = [
    {
        "code": "manufacturer_address",
        "name": "Manufacturer / packer / importer name and address",
        "legal_reference": "Legal Metrology (Packaged Commodities) Rules, 2011, Rule 6(1)(a)",
        "legal_note": "The package must identify the responsible manufacturer, packer, or importer with the applicable address details.",
        "description": "Check the package for the applicable manufacturer, packer, and (for imports) importer name and address declarations.",
        "weight": 3,
        "fix_suggestion": "Add the applicable manufacturer, packer, and importer name and complete address declarations, clearly identifying each role.",
    },
    {
        "code": "generic_name",
        "name": "Common or generic name of commodity",
        "legal_reference": "Legal Metrology (Packaged Commodities) Rules, 2011, Rule 6(1)(b)",
        "legal_note": "The package should identify the commodity by its common or generic name.",
        "description": "Check that the commodity is identified by its common or generic name.",
        "weight": 2,
        "fix_suggestion": "Add a clear common or generic name for the commodity; identify each item in a multi-product package.",
    },
    {
        "code": "net_quantity",
        "name": "Net quantity",
        "legal_reference": "Legal Metrology (Packaged Commodities) Rules, 2011, Rule 6(1)(c)",
        "legal_note": "The declared net quantity must use the applicable standard unit or count.",
        "description": "Check for net quantity in the applicable standard unit of weight or measure, or by number.",
        "weight": 3,
        "fix_suggestion": "Declare the net quantity in the applicable standard unit of weight or measure, or state the number of items.",
    },
    {
        "code": "mfg_date",
        "name": "Month and year of manufacture / pre-packing / import",
        "legal_reference": "Legal Metrology (Packaged Commodities) Rules, 2011, Rule 6(1)(d)",
        "legal_note": "Manufacture, pre-packing, or import month and year declarations are subject to product-specific exceptions.",
        "description": "Check for the month and year of manufacture, pre-packing, or import, subject to applicable product-specific exceptions and laws.",
        "weight": 2,
        "fix_suggestion": "Add the month and year of manufacture, pre-packing, or import when required; confirm any product-specific exception applies before omitting it.",
    },
    {
        "code": "mrp",
        "name": "Maximum retail price inclusive of taxes",
        "legal_reference": "Legal Metrology (Packaged Commodities) Rules, 2011, Rule 6(1)(e)",
        "legal_note": "The retail sale price declaration is subject to applicable exceptions and should include all taxes.",
        "description": "Check that the retail sale price is identified as the maximum retail price and is inclusive of all taxes, subject to applicable exceptions.",
        "weight": 3,
        "fix_suggestion": "Declare the MRP in rupees, clearly marked as the maximum retail price and inclusive of all taxes.",
    },
    {
        "code": "expiry_date",
        "name": "Expiry / best-before date, where declared",
        "legal_reference": "Product-specific requirements may apply",
        "legal_note": "Check readability when a product carries a shelf-life or expiry declaration; requirements vary by product category.",
        "description": "Assess expiry declarations only when detected or when the configured product category carries one.",
        "weight": 2,
        "fix_suggestion": "Ensure any required expiry or best-before date is legible and unambiguous.",
    },
    {
        "code": "consumer_care",
        "name": "Consumer complaint contact details",
        "legal_reference": "Legal Metrology (Packaged Commodities) Rules, 2011, Rule 6(2)",
        "legal_note": "Consumer complaint contact details must identify the responsible person or office.",
        "description": "Check for the name, address, telephone number, and email address of the person or office handling consumer complaints.",
        "weight": 2,
        "fix_suggestion": "Add the responsible person or office name, postal address, telephone number, and email address for consumer complaints.",
    },
    {
        "code": "country_of_origin",
        "name": "Country of origin for imported products",
        "legal_reference": "Legal Metrology (Packaged Commodities) Rules, 2011, Rule 6(1)(aa), as amended",
        "legal_note": "Country of origin is required for imported packages under the applicable rules.",
        "description": "For imported products, check for the country of origin, manufacture, or assembly declaration required by the applicable current rules.",
        "weight": 2,
        "fix_suggestion": "For an imported product, add a clear country of origin, manufacture, or assembly declaration as applicable.",
    },
]


class Command(BaseCommand):
    help = "Create or update the built-in packaged commodity compliance rules."

    def handle(self, *args, **options):
        for defaults in RULES:
            code = defaults["code"]
            _, created = Rule.objects.update_or_create(code=code, defaults=defaults)
            self.stdout.write(f"{'Created' if created else 'Updated'} {code}")
        self.stdout.write(self.style.SUCCESS(f"Seeded {len(RULES)} rules."))
