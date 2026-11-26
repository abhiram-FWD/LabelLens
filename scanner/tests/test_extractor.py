from django.test import SimpleTestCase

from scanner.services.extractor import extract_fields
from scanner.services.pipeline import _merge_fields


class ExtractFieldsTests(SimpleTestCase):
    LABELS = [
        (
            "Pure Cow Ghee\nNet Wt. 500 g\nMRP Rs. 320.00 (Inclusive of all taxes)\n"
            "Mfg: 04/2025\nBest Before: 12/2025\nMade in India\nPIN 560001",
            {"net_quantity": "500 g", "mrp": "320.00", "mfg_date": "04/2025", "expiry_date": "12/2025"},
        ),
        (
            "ALMOND DRINK\nNet Qty: 1 L\nM.R.P. â‚¹ 145\nPacked on 06-25\n"
            "Manufactured by: Example Foods Pvt Ltd\n12 Market Road, Mumbai 400001",
            {"net_quantity": "1 l", "mrp": "145", "mfg_date": "06-25", "manufacturer": "Example Foods Pvt Ltd; 12 Market Road, Mumbai 400001"},
        ),
        (
            "Herbal Face Wash\nNet Content 100 ml\nMRP: 99.50 inclusive of all taxes\n"
            "Mfd. by Green Labs, Pune 411001\nCustomer Care: 1800-123-4567 care@green.example",
            {"net_quantity": "100 ml", "mrp": "99.50", "manufacturer": "Green Labs, Pune 411001", "phone": "18001234567", "email": "care@green.example"},
        ),
        (
            "Basmati Rice\nNet Wt 5 kg\nMRP Rs. 650\nMfg 11/2024\nProduct of India\n"
            "Packed by: Grain House, Karnal 132001",
            {"net_quantity": "5 kg", "mrp": "650", "mfg_date": "11/2024", "country_of_origin": "India", "packed_by": "Grain House, Karnal 132001"},
        ),
        (
            "Chocolate Biscuits\nNet Qty 150 g\nMRP Rs 40/-\nManufactured by ABC Bakers, Delhi 110001\n"
            "Toll Free 1800 222 3333",
            {"net_quantity": "150 g", "mrp": "40", "manufacturer": "ABC Bakers, Delhi 110001"},
        ),
        (
            "Olive Oil\nNet Wt. 250 ml\nM.R.P Rs. 875\nImported by: Fresh Imports, Bengaluru 560034\n"
            "Made in Spain\nCustomer Care: +91 9876543210 help@example.com",
            {"net_quantity": "250 ml", "mrp": "875", "imported_by": "Fresh Imports, Bengaluru 560034", "country_of_origin": "Spain", "phone": "919876543210", "email": "help@example.com"},
        ),
        (
            "N3t Qty: 200 m1\nM.R.P. Rs. 75.00\nMfg: 03/2025\nExp: 03/2027\n"
            "Manufactured by: N0va Care Pvt Ltd\nMade in India",
            {"net_quantity": "200 ml", "mrp": "75.00", "mfg_date": "03/2025", "expiry_date": "03/2027", "manufacturer": "N0va Care Pvt Ltd", "country_of_origin": "India"},
        ),
        (
            "Wh0le Wheat Atta\nNet Wt 10 kg\nMRP Rs 520\nPacked on 09-24\n"
            "Marketed by: Sunrise Foods\nConsumer Care toll free 1800-111-2222\nPIN: 700001",
            {"net_quantity": "10 kg", "mrp": "520", "mfg_date": "09-24", "marketer": "Sunrise Foods", "phone": "18001112222"},
        ),
    ]

    def test_realistic_and_noisy_label_samples(self):
        self.assertGreaterEqual(len(self.LABELS), 8)
        for label, expected_fields in self.LABELS:
            with self.subTest(label=label[:35]):
                result = extract_fields(label)
                for name, value in expected_fields.items():
                    self.assertIn(name, result)
                    self.assertEqual(result[name]["value"], value)
                    self.assertGreaterEqual(result[name]["confidence"], 0)
                    self.assertLessEqual(result[name]["confidence"], 100)

    def test_generic_name_is_best_effort(self):
        result = extract_fields("Organic Turmeric Powder\nNet Qty 100 g\nMRP Rs 80")
        self.assertEqual(result["generic_name"]["value"], "Organic Turmeric Powder")

    def test_short_fuzzy_fragment_does_not_match_a_commodity(self):
        result = extract_fields("alt")
        self.assertNotIn("generic_name", result)
        self.assertNotEqual(result.get("generic_name", {}).get("value"), "Salt")

    def test_long_product_phrase_is_found_on_back_label_text(self):
        result = extract_fields("GOOD DAY BUTTER COOKIES")
        self.assertEqual(result["generic_name"]["value"], "Butter Cookies")

    def test_empty_text_returns_no_fields(self):
        self.assertEqual(extract_fields("  \n"), {})

    def test_parle_g_noisy_ocr_outputs_normalize_to_majority_values(self):
        label = """MRP %10.00
MRP 210.00
MRP =10.00
MRP <10.00
MRP Â£10.00
(Incl. of all taxes)
PKD: 10/2024
USE BY: 07/2025
BATCH NO.: G241010
FSSAI LICENCE NO: 10012011001234
Manufactured by: Parle Products Pvt Ltd
Vile Parle (East), Mumbal - 400 057, India.
Daman â€” 396 210, India.
Marketed by: Parle Products Pvt Ltd
Tel.: 022-6691 6929
Email: consumercare@parle. biz
consumercare@parle:biz
NET WEIGHT: 75g
NET WEIGHT: 759
NET WEIGHT: 715g
NET WEIGHT: 75 9
Original Gluco Biscuits
Parle-G
NET WEIGHT: 75 g
Barcode: 8901719110102"""
        fields = extract_fields(label)
        expected = {
            "mrp": "10.00",
            "net_quantity": "75 g",
            "mfg_date": "10/2024",
            "expiry_date": "07/2025",
            "phone": "02266916929",
            "email": "consumercare@parle.biz",
            "batch_number": "G241010",
            "fssai_license": "10012011001234",
            "barcode": "8901719110102",
            "mrp_inclusive_of_taxes": "Yes",
        }
        for name, value in expected.items():
            with self.subTest(field=name):
                self.assertEqual(fields[name]["value"], value)
        self.assertNotIn("net_quantity_conflict", fields)
        # Preserve OCR text instead of silently fuzzy-correcting a city name.
        self.assertIn("Mumbal", fields["manufacturer"]["value"])
        self.assertIn("396 210", fields["manufacturer"]["value"])
        self.assertNotIn("pincode_1", fields)
        self.assertEqual(fields["mrp_currency"]["value"], "INR")
        self.assertEqual(fields["brand"]["value"], "Parle-G")
        self.assertEqual(fields["generic_name"]["value"], "Gluco Biscuits")

    def test_consumer_care_phrases_are_detected_without_contact_details(self):
        fields = extract_fields("Consumer complaints: contact our Consumer Care Cell")
        self.assertNotIn("consumer_care", fields)

    def test_noisy_mrp_dates_addresses_and_separate_contact_fields(self):
        text = """MRP %10.00 (inclusive of all taxes)
Pkd. 10/09/2026
Mfg. 10/SEP/2026
Packed on Sep 2026
Use by 11/2027
Manufactured by: Parle Products Pvt Ltd
Village Parle, Mumbal district, Maharashtra - 400 057
Marketed by: Parle Marketing Ltd, Mumbai - 400057
Packed by: Pack Co, Pune 411001
Imported by: Import House, Daman - 396 210
Consumer Care Cell: Tel. 022-6691 6929
Email: care@example.com"""
        fields = extract_fields(text)
        self.assertEqual(fields["mrp"]["value"], "10.00")
        self.assertEqual(fields["mrp_currency"]["value"], "INR")
        self.assertEqual(fields["mrp_inclusive_of_taxes"]["value"], "Yes")
        self.assertEqual(fields["expiry_date"]["value"], "11/2027")
        for date_text, expected in (("Pkd. 10/2024", "10/2024"), ("Mfg. 10/09/2026", "10/09/2026"),
                                    ("Mfd. 10/SEP/2026", "10/SEP/2026"), ("Packed on Sep 2026", "Sep 2026")):
            self.assertEqual(extract_fields(date_text)["mfg_date"]["value"], expected)
        self.assertIn("Mumbal", fields["manufacturer"]["value"])
        for role in ("marketer", "packed_by", "imported_by"):
            self.assertIn(role, fields)
            self.assertNotEqual(fields.get("manufacturer", {}).get("value"), fields[role]["value"])
        self.assertEqual(fields["phone"]["value"], "02266916929")
        self.assertEqual(fields["email"]["value"], "care@example.com")
        self.assertFalse(any(name.startswith("pincode") for name in fields))

    def test_requested_ocr_regressions(self):
        fields = extract_fields("""MRP & 10.00 (INCL. OF ALL TAXES)
Pkd.: 10/SEP/2026
Use By: 09/MAR/2027
NET QUANTITY : 79.49""")
        self.assertEqual(fields["mrp"]["value"], "10.00")
        self.assertEqual(fields["mrp_inclusive_of_taxes"]["value"], "Yes")
        self.assertEqual(fields["mfg_date"]["value"], "10/SEP/2026")
        self.assertEqual(fields["expiry_date"]["value"], "09/MAR/2027")
        self.assertIn("Pkd", fields["mfg_date"]["evidence"])
        self.assertIn("Use By", fields["expiry_date"]["evidence"])
        self.assertEqual(fields["net_quantity"]["value"], "79.49")
        self.assertTrue(fields["net_quantity"]["review_reason"])

    def test_reversed_ocr_order_uses_date_headings_and_keeps_ambiguous_dates_for_review(self):
        fields = extract_fields("Use By\n09/MAR/2027\nPkd\n10/SEP/2026")
        self.assertEqual(fields["mfg_date"]["value"], "10/SEP/2026")
        self.assertEqual(fields["expiry_date"]["value"], "09/MAR/2027")
        ambiguous = extract_fields("Pkd\n10/SEP/2026\nUse By")
        self.assertTrue(ambiguous["mfg_date"]["review_reason"])

    def test_date_headings_before_and_after_dates_keep_declaration_roles(self):
        cases = (
            ("Pkd: 10/SEP/2026", "mfg_date", "10/SEP/2026"),
            ("10/SEP/2026 Pkd", "mfg_date", "10/SEP/2026"),
            ("Use By: 09/MAR/2027", "expiry_date", "09/MAR/2027"),
            ("09/MAR/2027 Use By", "expiry_date", "09/MAR/2027"),
        )
        for text, field_name, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(extract_fields(text)[field_name]["value"], expected)

    def test_close_heading_date_pairs_are_matched_globally(self):
        fields = extract_fields("Pkd\n10/SEP/2026\nUse By\n09/MAR/2027")
        self.assertEqual(fields["mfg_date"]["value"], "10/SEP/2026")
        self.assertEqual(fields["expiry_date"]["value"], "09/MAR/2027")

    def test_scan_8_exact_four_line_input_preserves_heading_date_evidence(self):
        fields = extract_fields("Pkd\n10/SEP/2026\nUse By\n09/MAR/2027")
        self.assertEqual(fields["mfg_date"]["value"], "10/SEP/2026")
        self.assertEqual(fields["expiry_date"]["value"], "09/MAR/2027")
        self.assertEqual(fields["mfg_date"]["evidence"], "Pkd\n10/SEP/2026")
        self.assertEqual(fields["expiry_date"]["evidence"], "Use By\n09/MAR/2027")

    def test_use_before_is_expiry_heading_before_or_after_date(self):
        for text in ("Use Before 09/MAR/2027", "09/MAR/2027 Use Before"):
            with self.subTest(text=text):
                field = extract_fields(text)["expiry_date"]
                self.assertEqual(field["value"], "09/MAR/2027")
                self.assertIn("Use Before", field["evidence"])

    def test_column_date_labels_keep_their_own_dates(self):
        text = "10/SEP/2026\nPkd\n09/MAR/2027\nUse By"
        lines = [
            {"text": "10/SEP/2026", "top": 10, "bottom": 20, "left": 10, "right": 90},
            {"text": "Pkd", "top": 25, "bottom": 35, "left": 10, "right": 40},
            {"text": "09/MAR/2027", "top": 10, "bottom": 20, "left": 210, "right": 290},
            {"text": "Use By", "top": 25, "bottom": 35, "left": 210, "right": 270},
        ]
        fields = extract_fields(text, ocr_lines=lines)
        self.assertEqual(fields["mfg_date"]["value"], "10/SEP/2026")
        self.assertEqual(fields["expiry_date"]["value"], "09/MAR/2027")

    def test_exact_scan_sample_keeps_date_mrp_quantity_and_evidence_separate(self):
        text = """79.49
NET QUANTITY
MRP & 10.00
(INCL. OF ALL TAXES)
UNIT SALE PRICE â‚¬ 0.13/g
10/SEP/2026
Pkd
09/MAR/2027
Use By
G259A
BATCH No
sonsumer.care@parle.biz
consumer.care@parle.biz"""
        lines = [
            {"text": "79.49", "confidence": 72},
            {"text": "NET QUANTITY", "confidence": 74},
            {"text": "MRP & 10.00", "confidence": 91},
            {"text": "(INCL. OF ALL TAXES)", "confidence": 87},
            {"text": "UNIT SALE PRICE â‚¬ 0.13/g", "confidence": 89},
            {"text": "10/SEP/2026", "confidence": 88},
            {"text": "Pkd", "confidence": 86},
            {"text": "09/MAR/2027", "confidence": 93},
            {"text": "Use By", "confidence": 92},
            {"text": "G259A", "confidence": 90},
            {"text": "BATCH No", "confidence": 91},
            {"text": "sonsumer.care@parle.biz", "confidence": 55},
            {"text": "consumer.care@parle.biz", "confidence": 88},
        ]
        fields = extract_fields(text, ocr_lines=lines)
        self.assertEqual(fields["mrp"]["value"], "10.00")
        self.assertEqual(fields["mfg_date"]["value"], "10/SEP/2026")
        self.assertEqual(fields["expiry_date"]["value"], "09/MAR/2027")
        self.assertEqual(fields["net_quantity"]["value"], "79.49")
        self.assertEqual(fields["net_quantity"]["confidence"], 73)
        self.assertEqual(fields["mfg_date"]["confidence"], 87)
        self.assertIn("NET QUANTITY", fields["net_quantity"]["evidence"])
        self.assertIn("Pkd", fields["mfg_date"]["evidence"])
        self.assertEqual(fields["unit_sale_price"]["value"], "0.13 / g")
        self.assertEqual(fields["batch_number"]["value"], "G259A")
        self.assertEqual(fields["email"]["value"], "consumer.care@parle.biz")

    def test_unit_sale_price_and_party_pin_codes_are_normalized_and_deduplicated(self):
        fields = extract_fields("""Unit Sale Price: Rs. 0.50/g
Manufactured by: Acme Foods
12 Road, Delhi 110001
Delhi PIN 110001""")
        self.assertEqual(fields["unit_sale_price"]["value"], "0.50 / g")
        self.assertEqual(fields["manufacturer"]["value"], "Acme Foods; 12 Road, Delhi 110001; Delhi PIN 110001")
        self.assertEqual(fields["pin_codes"]["value"], "110001")

    def test_manufacturer_block_stops_at_next_heading_and_roles_stay_separate(self):
        fields = extract_fields("""Manufactured by: Acme Foods
12 Road, Delhi 110001
Marketed by: Acme Retail
For consumer complaints: call 1800 123 4567
MRP & 10.00""")
        self.assertEqual(fields["manufacturer"]["value"], "Acme Foods; 12 Road, Delhi 110001")
        self.assertEqual(fields["marketer"]["value"], "Acme Retail")
        self.assertEqual(fields["pin_codes"]["value"], "110001")
        self.assertNotIn("packed_by", fields)

    def test_party_addresses_skip_interleaved_dates_and_keep_manufacturer_separate(self):
        fields = extract_fields("""Manufactured by: Acme Foods Ltd.
Plot 1, Industrial Estate, Delhi
10/SEP/2026
Pkd
PIN 110001, India
Marketed by: Acme Retail
Tower 2, Mumbai 400001""")
        self.assertNotIn("10/SEP/2026", fields["manufacturer"]["value"])
        self.assertIn("110001", fields["manufacturer"]["value"])
        self.assertNotIn("Mumbai", fields["manufacturer"]["value"])
        self.assertIn("Acme Retail", fields["marketer"]["value"])

    def test_repeated_party_blocks_are_deduplicated_but_source_noise_is_preserved(self):
        block = """Marketed by: Acme Retail Ltd.
Prestige Tower, Bengaluru 560048
&
Marketed by: Acme Retail Ltd.
Prestige Tower, Bengaluru 560048
ISSQL"""
        fields = extract_fields(block)
        self.assertEqual(fields["marketer"]["value"].count("Prestige Tower"), 1)
        self.assertEqual(fields["marketer"]["evidence"].count("Prestige Tower"), 1)
        self.assertNotIn("ISSQL", fields["marketer"]["value"])
        self.assertIn("ISSQL", fields["marketer"]["evidence"])
        self.assertIn("560048", fields["marketer"]["value"])

    def test_two_column_text_with_interleaved_lines_extracts_both_columns(self):
        text = """NET QUANTITY: 500 g
MRP & 10.00
Pkd.: 10/SEP/2026
Manufactured by: North Foods
12 Main Road, Delhi 110001
Marketed by: North Retail"""
        fields = extract_fields(text)
        self.assertEqual(fields["net_quantity"]["value"], "500 g")
        self.assertEqual(fields["mrp"]["value"], "10.00")
        self.assertIn("12 Main Road", fields["manufacturer"]["value"])

    def test_pipeline_merge_prefers_the_highest_confidence_pass(self):
        merged = _merge_fields([
            ({"mrp": {"value": "10.00", "confidence": 55}}, 80),
            ({"mrp": {"value": "10.00", "confidence": 60}}, 82),
            ({"mrp": {"value": "210.00", "confidence": 99}}, 99),
        ])
        self.assertEqual(merged["mrp"]["value"], "210.00")
