import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.context_builder import PromptBudgetExceeded, build_prompt
from app.llm import (
    ProviderRequest,
    _normalize_usage,
    _post_payload,
    _provider_error_category,
    _retry_after_seconds,
    _safe_provider_user_message,
    call_llm,
    get_provider_adapter,
    get_model_config,
)
from app.provider_registry import model_capabilities, model_options
from app.workbook import sample_csv_context


class ProviderRegistryTests(unittest.TestCase):
    def test_unknown_model_limits_remain_unknown(self):
        capabilities = model_capabilities("unknown-model")

        self.assertIsNone(capabilities["context_window_tokens"])
        self.assertIsNone(capabilities["max_output_tokens"])
        self.assertEqual(capabilities["modalities"], [])

    def test_configured_limits_and_enabled_provider_filtering(self):
        with patch.dict(
            os.environ,
            {
                "PRIVY_MODEL_METADATA": (
                    '{"groq-gpt-oss-20b":{"context_window_tokens":8192,'
                    '"max_output_tokens":2048}}'
                ),
                "GROQ_API_KEY": "test-key",
                "GEMINI_API_KEY": "",
                "PRIVY_DISABLED_PROVIDERS": "",
            },
            clear=False,
        ):
            capabilities = model_capabilities("groq-gpt-oss-20b")
            options = model_options()

        self.assertEqual(capabilities["context_window_tokens"], 8192)
        self.assertEqual(capabilities["max_output_tokens"], 2048)
        self.assertEqual({item["provider"] for item in options}, {"groq"})
        self.assertEqual(len(options), 2)
        self.assertIn("capabilities", options[0])

    def test_disabled_provider_is_rejected_by_backend_model_resolution(self):
        with patch.dict(
            os.environ,
            {"GROQ_API_KEY": "test-key", "PRIVY_DISABLED_PROVIDERS": "groq"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                get_model_config("groq-gpt-oss-20b")


class PromptBudgetTests(unittest.TestCase):
    def test_preserves_current_question_and_limits_history_and_file_context(self):
        built = build_prompt(
            system_prompt="Follow the system instructions.",
            file_context="row " * 1000,
            history="previous turn " * 400,
            question="What is the current answer?",
            requested_output_tokens=100,
            model_capabilities={"context_window_tokens": 500, "max_output_tokens": 100},
        )

        self.assertIn("What is the current answer?", built.user_prompt)
        self.assertTrue(built.context_limited)
        self.assertLessEqual(built.estimated_input_tokens + built.reserved_output_tokens, 500)

    def test_rejects_fixed_prompt_that_cannot_fit(self):
        with self.assertRaises(PromptBudgetExceeded):
            build_prompt(
                system_prompt="Large fixed system prompt " * 100,
                file_context="",
                history="",
                question="Question",
                requested_output_tokens=50,
                model_capabilities={"context_window_tokens": 100, "max_output_tokens": 50},
            )


class ProviderNormalizationTests(unittest.TestCase):
    def test_usage_normalizes_openai_compatible_fields(self):
        usage = _normalize_usage(
            "groq",
            "openai/gpt-oss-20b",
            {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "prompt_tokens_details": {"cached_tokens": 10},
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        )

        self.assertEqual(usage["input_tokens"], 120)
        self.assertEqual(usage["output_tokens"], 30)
        self.assertEqual(usage["cached_input_tokens"], 10)
        self.assertEqual(usage["reasoning_tokens"], 5)
        self.assertEqual(usage["source"], "provider_reported")

    def test_error_categories_and_retry_after_are_normalized(self):
        self.assertEqual(_provider_error_category(429), "quota")
        self.assertEqual(_provider_error_category(400, "maximum context length"), "context_window")
        self.assertEqual(_provider_error_category(503), "provider_unavailable")
        self.assertEqual(_retry_after_seconds({"Retry-After": "12"}), 12.0)
        self.assertIsNone(_retry_after_seconds({"Retry-After": "invalid"}))
        self.assertFalse(_safe_provider_user_message("groq", 401)[1])

    def test_groq_usage_reporting_is_requested_for_streaming(self):
        with patch.dict(os.environ, {"PRIVY_STREAM_USAGE_REPORTING": "true"}):
            payload = _post_payload("system", "question", "model", 0.1, 100, True, "groq")

        self.assertEqual(payload["stream_options"], {"include_usage": True})

    def test_registered_adapters_expose_common_stream_and_completion_contract(self):
        adapter = get_provider_adapter("groq")
        request = ProviderRequest(
            system_prompt="system",
            user_prompt="question",
            api_key="test-key",
            model="openai/gpt-oss-20b",
            provider="groq",
            temperature=0.1,
            max_tokens=20,
            file_refs=[],
        )

        self.assertTrue(callable(adapter.stream))
        self.assertTrue(callable(adapter.complete))
        self.assertEqual(request.provider, "groq")

    def test_completion_facade_dispatches_through_the_provider_adapter(self):
        adapter = Mock()
        adapter.complete.return_value = "answer"
        with patch.dict(
            os.environ,
            {"GROQ_API_KEY": "test-key", "PRIVY_DISABLED_PROVIDERS": ""},
            clear=False,
        ), patch("app.llm.get_provider_adapter", return_value=adapter):
            answer = call_llm(
                "system",
                "question",
                api_key=None,
                model="groq-gpt-oss-20b",
                max_tokens=50,
            )

        self.assertEqual(answer, "answer")
        adapter.complete.assert_called_once()


class WorkbookContextTests(unittest.TestCase):
    def test_relevant_sample_preserves_csv_structure(self):
        sample = sample_csv_context(
            "Region,Amount,Internal Note\n"
            "East,12,irrelevant-a\n"
            "West,25,irrelevant-b\n",
            "Region West",
        )

        self.assertIn("Region", sample)
        self.assertIn("3,West", sample)
        self.assertNotIn("Internal Note", sample)
        self.assertNotIn("irrelevant-a", sample)

    def test_sample_escapes_embedded_newlines_before_prompt_truncation(self):
        sample = sample_csv_context(
            'Name,Note\nAlice,"line one\nline two"\n',
            "line",
        )

        self.assertIn(r"line one\nline two", sample)
        self.assertEqual(sample.count("\n"), 3)


if __name__ == "__main__":
    unittest.main()
