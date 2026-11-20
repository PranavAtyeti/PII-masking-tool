import json
import os
import sys
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.llm import ProviderRequest, get_provider_adapter
from app.masking import stream_unmask_events


def _response(*chunks):
    response = Mock()
    response.status_code = 200
    response.headers = {}
    response.iter_lines.return_value = [
        *(f"data: {json.dumps(chunk)}" for chunk in chunks),
        "data: [DONE]",
    ]
    return response


class ProviderToolLoopTests(unittest.TestCase):
    def test_tool_status_events_do_not_interrupt_token_unmasking_or_unmask_results(self):
        events = [
            {"type": "delta", "text": "Hello [PERSON_"},
            {"type": "tool_start", "name": "get_column_profile"},
            {"type": "tool_complete", "name": "get_column_profile"},
            {"type": "delta", "text": "1]!"},
        ]
        with patch("app.masking.store.get_reverse_map", return_value={"[PERSON_1]": "Alice"}):
            output = list(stream_unmask_events(events, "chat-1"))

        text = "".join(event.get("text", "") for event in output if event["type"] == "delta")
        self.assertEqual(text, "Hello Alice!")
        self.assertEqual([event["type"] for event in output if event["type"] != "delta"], ["tool_start", "tool_complete"])

    def test_gemini_and_groq_share_tool_call_contract(self):
        tool_chunk = {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "aggregate_data",
                            "arguments": '{"aggregation":"count"}',
                        },
                    }]
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35},
        }
        final_chunk = {
            "choices": [{
                "delta": {"content": "There are 251 rows."},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 40, "completion_tokens": 8, "total_tokens": 48},
        }
        schemas = [{
            "name": "aggregate_data",
            "description": "Count workbook rows.",
            "parameters": {
                "type": "object",
                "properties": {"aggregation": {"type": "string"}},
            },
        }]

        for provider, model in [
            ("groq", "openai/gpt-oss-20b"),
            ("gemini", "gemini-3.7-flash"),
        ]:
            with self.subTest(provider=provider):
                first_usage = (
                    {"total_input_tokens": 30, "total_output_tokens": 5, "total_tokens": 35}
                    if provider == "gemini"
                    else {"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35}
                )
                final_usage = (
                    {"total_input_tokens": 40, "total_output_tokens": 8, "total_tokens": 48}
                    if provider == "gemini"
                    else {"prompt_tokens": 40, "completion_tokens": 8, "total_tokens": 48}
                )
                first_chunk = deepcopy(tool_chunk)
                second_chunk = deepcopy(final_chunk)
                first_chunk["usage"] = first_usage
                second_chunk["usage"] = final_usage
                responses = [_response(first_chunk), _response(second_chunk)]
                usage = {}
                request = ProviderRequest(
                    system_prompt="Use local tools.",
                    user_prompt="Count rows.",
                    api_key="test-key",
                    model=model,
                    provider=provider,
                    temperature=0.1,
                    max_tokens=200,
                    file_refs=[],
                    tool_schemas=schemas,
                    tool_executor=lambda name, arguments: {
                        "ok": True,
                        "result": {"value": 251},
                    },
                    max_tool_rounds=2,
                    max_tool_calls=3,
                )
                with patch("app.llm.get_model_config", return_value=(provider, "https://provider.test/chat/completions", "test-key", model)), patch(
                    "app.llm.requests.post",
                    side_effect=responses,
                ) as post:
                    events = list(get_provider_adapter(provider).stream_events(request, usage))

                self.assertEqual(
                    [event["type"] for event in events],
                    ["tool_start", "tool_complete", "delta"],
                )
                self.assertEqual(events[-1]["text"], "There are 251 rows.")
                self.assertEqual(usage["total_tokens"], 83)
                self.assertEqual(usage["input_tokens"], 70)
                self.assertEqual(usage["output_tokens"], 13)
                self.assertEqual(post.call_count, 2)
                self.assertEqual(
                    post.call_args_list[0].kwargs["headers"]["Authorization"],
                    "Bearer test-key",
                )
                self.assertEqual(
                    post.call_args_list[0].kwargs["json"]["tools"][0]["function"]["name"],
                    "aggregate_data",
                )
                follow_up_messages = post.call_args_list[1].kwargs["json"]["messages"]
                self.assertTrue(any(message.get("role") == "tool" for message in follow_up_messages))
                self.assertTrue(all(response.close.called for response in responses))

    def test_tool_round_limit_stops_before_another_provider_request(self):
        response = _response({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call-1",
                        "function": {"name": "aggregate_data", "arguments": "{}"},
                    }]
                },
                "finish_reason": "tool_calls",
            }]
        })
        request = ProviderRequest(
            system_prompt="system",
            user_prompt="question",
            api_key="test-key",
            model="openai/gpt-oss-20b",
            provider="groq",
            temperature=0.1,
            max_tokens=100,
            file_refs=[],
            tool_schemas=[{"name": "aggregate_data", "description": "Count", "parameters": {"type": "object"}}],
            tool_executor=lambda _name, _arguments: {"ok": True, "result": {}},
            max_tool_rounds=0,
            max_tool_calls=1,
        )
        with patch("app.llm.get_model_config", return_value=("groq", "https://provider.test/chat/completions", "test-key", request.model)), patch(
            "app.llm.requests.post",
            return_value=response,
        ) as post:
            with self.assertRaisesRegex(Exception, "exceeded the configured tool round limit"):
                list(get_provider_adapter("groq").stream_events(request))

        self.assertEqual(post.call_count, 1)
        self.assertTrue(response.close.called)

    def test_each_tool_round_respects_known_context_budget(self):
        request = ProviderRequest(
            system_prompt="a system instruction " * 100,
            user_prompt="a question " * 100,
            api_key="test-key",
            model="openai/gpt-oss-20b",
            provider="groq",
            temperature=0.1,
            max_tokens=100,
            file_refs=[],
            tool_schemas=[{"name": "aggregate_data", "description": "Count", "parameters": {"type": "object"}}],
            tool_executor=lambda _name, _arguments: {},
            max_tool_rounds=1,
            max_tool_calls=1,
        )
        with patch("app.llm.get_model_config", return_value=("groq", "https://provider.test/chat/completions", "test-key", request.model)), patch(
            "app.llm.model_capabilities",
            return_value={"context_window_tokens": 100, "max_output_tokens": 100},
        ), patch("app.llm.requests.post") as post:
            with self.assertRaisesRegex(Exception, "exceeded the configured prompt budget"):
                list(get_provider_adapter("groq").stream_events(request))

        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
