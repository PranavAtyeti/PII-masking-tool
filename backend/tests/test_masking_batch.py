"""Regression tests for deduplicated spreadsheet masking."""

import os
import sys
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
from fastapi import HTTPException

from app.privacy_policy import (
    build_outbound_csv,
    policy_json,
    validate_policy,
)
from app.crypto import decrypt_text, encrypt_text, generate_key, is_encrypted

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

    def test_privacy_policy_exclude_and_confirmed_sensitive_share(self):
        policy = validate_policy(
            ["Email", "Department", "Salary", "Notes"],
            {
                "Email": "EMAIL",
                "Department": None,
                "Salary": "AMOUNT",
                "Notes": None,
            },
            {
                "Email": {"action": "share", "share_confirmed": True},
                "Department": {"action": "share", "share_confirmed": False},
                "Salary": {"action": "share", "share_confirmed": False},
                "Notes": {"action": "exclude", "share_confirmed": False},
            },
        )
        csv_text = "Email,Department,Salary,Notes\nrohan@example.com,Engineering,95000,secret\n"
        outbound = build_outbound_csv(csv_text, policy)
        self.assertIn("rohan@example.com", outbound)
        self.assertIn("Department", outbound)
        self.assertIn("Salary", outbound)
        self.assertNotIn("Notes", outbound)
        self.assertNotIn("secret", outbound)
        self.assertIn('"columns"', policy_json(policy))

    def test_sensitive_share_requires_confirmation(self):
        with self.assertRaises(HTTPException) as context:
            validate_policy(
                ["Bank Account"],
                {"Bank Account": "ID"},
                {"Bank Account": {"action": "share", "share_confirmed": False}},
            )
        self.assertEqual(context.exception.status_code, 422)

    def test_masked_output_rejects_unmasked_pii(self):
        policy = {
            "Email": {
                "action": "mask",
                "share_confirmed": False,
                "sensitive": True,
            }
        }
        with self.assertRaises(HTTPException):
            build_outbound_csv(
                "Email\nrohan@example.com\n",
                policy,
            )


class EncryptionTests(unittest.TestCase):
    def setUp(self):
        os.environ["PRIVY_ENVIRONMENT"] = "development"
        os.environ["PRIVY_ENCRYPTION_KEY"] = generate_key()

    def tearDown(self):
        os.environ.pop("PRIVY_ENCRYPTION_KEY", None)

    def test_encryption_round_trip_and_unique_ciphertext(self):
        first = encrypt_text("Rohan Mehta", aad="test:a")
        second = encrypt_text("Rohan Mehta", aad="test:a")
        self.assertTrue(is_encrypted(first))
        self.assertNotEqual(first, second)
        self.assertEqual(decrypt_text(first, aad="test:a"), "Rohan Mehta")

    def test_tampering_is_rejected(self):
        ciphertext = encrypt_text("Rohan Mehta", aad="test:a")
        tampered = ciphertext[:-2] + ("A" if ciphertext[-1] != "A" else "B")
        with self.assertRaises(Exception):
            decrypt_text(tampered, aad="test:a")

    def test_legacy_plaintext_remains_readable(self):
        self.assertEqual(decrypt_text("legacy plaintext", aad="test:a"), "legacy plaintext")

    def test_production_kms_requires_configuration(self):
        os.environ["PRIVY_ENVIRONMENT"] = "production"
        os.environ.pop("PRIVY_KMS_KEY_NAME", None)
        with self.assertRaises(Exception):
            encrypt_text("Rohan Mehta", aad="test:a")


if __name__ == "__main__":
    unittest.main()
