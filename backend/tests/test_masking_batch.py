"""Regression tests for deduplicated spreadsheet masking."""

import os
import sys
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd

from app import masking


class MaskingBatchTests(unittest.TestCase):
    def setUp(self):
        self.original_bulk_tokens = masking.store.get_or_create_tokens
        self.original_analyze_texts = masking.ner_detection.analyze_texts
        self.tokens: dict[str, str] = {}
        self.bulk_calls: list[list[tuple[str, str]]] = []
        self.ner_inputs: list[list[str]] = []

        def fake_bulk_tokens(_session_id, values, counters):
            values = list(values)
            self.bulk_calls.append(values)
            for value_type, raw_value in values:
                normalized = str(raw_value).strip().lower()
                if normalized not in self.tokens:
                    counters[value_type] = counters.get(value_type, 0) + 1
                    self.tokens[normalized] = f"[{value_type}_{counters[value_type]}]"
            return self.tokens.copy()

        def fake_analyze_texts(texts, _confidence):
            unique = list(dict.fromkeys(texts))
            self.ner_inputs.append(unique)
            return {
                text: [("Rohan Mehta", "PERSON", 0.95)]
                if "Rohan Mehta" in text
                else []
                for text in unique
            }

        masking.store.get_or_create_tokens = fake_bulk_tokens
        masking.ner_detection.analyze_texts = fake_analyze_texts

    def tearDown(self):
        masking.store.get_or_create_tokens = self.original_bulk_tokens
        masking.ner_detection.analyze_texts = self.original_analyze_texts

    def test_repeated_structured_and_free_text_values_are_masked_once(self):
        df = pd.DataFrame(
            {
                "Email": ["rohan@example.com", "rohan@example.com"],
                "Notes": [
                    "Please contact Rohan Mehta at rohan@example.com today.",
                    "Please contact Rohan Mehta at rohan@example.com today.",
                ],
            }
        )

        masked, known_values = masking.mask_dataframe(
            df,
            {"Email": "EMAIL", "Notes": None},
            session_id="test-chat",
            counters={},
            use_ner=True,
            ner_confidence=0.6,
        )

        self.assertEqual(masked.at[0, "Email"], masked.at[1, "Email"])
        self.assertNotIn("rohan@example.com", masked.at[0, "Notes"])
        self.assertNotIn("Rohan Mehta", masked.at[0, "Notes"])
        self.assertEqual(masked.at[0, "Notes"], masked.at[1, "Notes"])
        self.assertEqual(len(self.ner_inputs), 1)
        self.assertEqual(len(self.ner_inputs[0]), 1)
        self.assertEqual(len(self.tokens), 2)
        self.assertIn("rohan@example.com", known_values)


if __name__ == "__main__":
    unittest.main()
