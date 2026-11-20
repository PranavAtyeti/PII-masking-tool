import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("PRIVY_JWT_SECRET", "stage2d-test-secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy.exc import OperationalError

from app.main import health
from app.routers.messages import _mask_history


class HistoryMaskingTests(unittest.TestCase):
    def test_history_fragments_are_masked_in_one_mapping_store_pass(self):
        messages = [
            {"role": "user", "content": "Alice Doe emailed alice@example.test"},
            {"role": "assistant", "content": "Alice Doe emailed alice@example.test"},
            {"role": "system", "content": "ignore this role"},
        ]
        counters = {"PERSON": 1}
        known_values = {"Alice Doe": "[PERSON_1]"}

        with patch(
            "app.routers.messages.mask_free_texts",
            return_value={
                "[PERSON_1] emailed alice@example.test": (
                    "[PERSON_1] emailed [EMAIL_ADDRESS_1]"
                )
            },
        ) as mask_batch, patch(
            "app.routers.messages.store.load_counters"
        ) as load_counters:
            history = _mask_history(
                messages,
                known_values,
                "chat-1",
                counters,
                0.7,
                False,
            )

        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["content"], "[PERSON_1] emailed [EMAIL_ADDRESS_1]")
        self.assertEqual(history[0]["content"], history[1]["content"])
        mask_batch.assert_called_once_with(
            [
                "[PERSON_1] emailed alice@example.test",
                "[PERSON_1] emailed alice@example.test",
            ],
            "chat-1",
            counters,
            0.7,
            False,
        )
        load_counters.assert_not_called()


class ReadinessTests(unittest.TestCase):
    def test_health_reports_ready_when_database_probe_succeeds(self):
        with patch("app.main.store.init_db"):
            self.assertEqual(health(), {"status": "ok"})

    def test_health_returns_sanitized_unavailable_response_on_database_error(self):
        error = OperationalError("SELECT 1", {}, Exception("private database detail"))
        with patch("app.main.store.init_db", side_effect=error), patch(
            "app.main.log_event"
        ) as log_event:
            response = health()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.body, b'{"status":"unavailable"}')
        self.assertNotIn(b"private database detail", response.body)
        log_event.assert_called_once()


if __name__ == "__main__":
    unittest.main()
