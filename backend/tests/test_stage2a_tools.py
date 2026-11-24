import json
import os
import sys
import unittest
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.privacy_policy import policy_json
from app.tools import ToolContext, ToolExecutionError, _source_identity, cleanup_tool_context, execute_tool, provider_tool_schemas
from app.workbook import build_workbook_document, column_identity, parse_workbook, serialize_workbook


class WorkbookToolTests(unittest.TestCase):
    def setUp(self):
        self.file_id = "workbook-1"
        self.chat_id = "chat-1"
        self.column_ids = {
            name: column_identity(0, index, name)
            for index, name in enumerate(["Department", "Amount", "Person", "Excluded"])
        }
        row_count = 251
        csv_rows = ["Department,Amount,Person,Excluded"]
        csv_rows.extend(
            f"{'Sales' if index % 2 else 'Support'},{index + 1}.1234567890123456789,[PERSON_1],private-{index}"
            for index in range(row_count)
        )
        parsed = parse_workbook("records.csv", "\n".join(csv_rows).encode())
        frame = parsed.worksheets[0].dataframe.copy()
        frame["Person"] = "[PERSON_1]"
        frame.loc[0, "Person"] = None
        policy = {
            "Department": {
                "action": "share",
                "sensitive": False,
                "share_confirmed": False,
            },
            "Amount": {
                "action": "share",
                "sensitive": False,
                "share_confirmed": False,
            },
            "Person": {
                "action": "mask",
                "sensitive": True,
                "share_confirmed": False,
            },
            "Excluded": {
                "action": "exclude",
                "sensitive": False,
                "share_confirmed": False,
            },
        }
        types = {
            "sheet-1": {
                "Department": None,
                "Amount": None,
                "Person": "PERSON",
                "Excluded": None,
            }
        }
        document = build_workbook_document(
            parsed,
            self.file_id,
            policy,
            types,
            {"sheet-1": frame},
        )
        self.chat_file = {
            "file_id": self.file_id,
            "updated_at": 1.0,
            "workbook_json": serialize_workbook(document),
            "privacy_policy_json": policy_json(policy),
            "columns_json": json.dumps([
                {"name": name, "type": types["sheet-1"][name]}
                for name in types["sheet-1"]
            ]),
            "masked_csv": "",
            "row_count": row_count,
        }

    def execute(self, name, args):
        context = ToolContext(
            user_id="user-1",
            chat_id=self.chat_id,
            files={self.file_id: self.chat_file},
        )
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions",
            return_value={self.file_id: 1.0},
        ):
            return execute_tool(context, name, args)

    def test_schema_hides_excluded_columns_and_values(self):
        result = self.execute("get_workbook_schema", {"file_id": self.file_id})

        schema = result["worksheets"][0]
        self.assertEqual(schema["row_count"], 251)
        self.assertEqual(
            {column["name"] for column in schema["columns"]},
            {"Department", "Amount", "Person"},
        )
        self.assertNotIn("private-0", json.dumps(result))

    def test_aggregate_uses_all_rows_and_preserves_decimal_precision(self):
        result = self.execute(
            "aggregate_data",
            {
                "file_id": self.file_id,
                "worksheet_id": "sheet-1",
                "aggregation": "sum",
                "column_id": self.column_ids["Amount"],
            },
        )

        self.assertTrue(result["exact"])
        self.assertEqual(result["row_count"], 251)
        expected = sum(
            (Decimal(f"{index}.1234567890123456789") for index in range(1, 252)),
            Decimal(0),
        )
        self.assertEqual(result["value"], format(expected, "f"))

    def test_grouping_and_filtering_return_only_permitted_columns(self):
        grouped = self.execute(
            "group_and_aggregate",
            {
                "file_id": self.file_id,
                "worksheet_id": "sheet-1",
                "group_column_id": self.column_ids["Department"],
                "aggregation": "sum",
                "metric_column_id": self.column_ids["Amount"],
            },
        )
        filtered = self.execute(
            "filter_rows",
            {
                "file_id": self.file_id,
                "worksheet_id": "sheet-1",
                "conditions": [{
                    "column_id": self.column_ids["Department"],
                    "operator": "eq",
                    "value": "Sales",
                }],
                "columns": [self.column_ids["Department"], self.column_ids["Person"]],
                "limit": 5,
            },
        )

        self.assertEqual(grouped["group_count"], 2)
        self.assertTrue(grouped["exact"])
        self.assertEqual(filtered["matched_row_count"], 125)
        self.assertTrue(filtered["truncated"])
        self.assertNotIn("private-", json.dumps(filtered))
        self.assertNotIn("[PERSON_1]", json.dumps(filtered))
        self.assertTrue(all(row["values"][self.column_ids["Person"]] == "[MASKED]" for row in filtered["rows"]))

    def test_sensitive_profile_omits_raw_statistics_and_category_values(self):
        result = self.execute(
            "get_column_profile",
            {
                "file_id": self.file_id,
                "worksheet_id": "sheet-1",
                "column_id": self.column_ids["Person"],
            },
        )

        self.assertEqual(result["total_rows"], 251)
        self.assertEqual(result["non_null_count"], 250)
        self.assertEqual(result["null_count"], 1)
        self.assertNotIn("minimum", result)
        self.assertNotIn("maximum", result)
        self.assertNotIn("top_categories", result)
        self.assertNotIn("[PERSON_1]", json.dumps(result))

    def test_excluded_column_and_invalid_aggregation_are_rejected(self):
        with self.assertRaises(ToolExecutionError) as excluded:
            self.execute(
                "get_column_profile",
                {
                    "file_id": self.file_id,
                    "worksheet_id": "sheet-1",
                    "column_id": self.column_ids["Excluded"],
                },
            )
        self.assertEqual(excluded.exception.code, "column_not_found")

        with self.assertRaises(ToolExecutionError) as invalid:
            self.execute(
                "aggregate_data",
                {
                    "file_id": self.file_id,
                    "worksheet_id": "sheet-1",
                    "aggregation": "median",
                    "column_id": self.column_ids["Amount"],
                },
            )
        self.assertEqual(invalid.exception.code, "invalid_aggregation")

    def test_compare_groups_returns_exact_bounded_results(self):
        result = self.execute(
            "compare_groups",
            {
                "file_id": self.file_id,
                "worksheet_id": "sheet-1",
                "group_column_id": self.column_ids["Department"],
                "group_values": ["Sales", "Support"],
                "aggregation": "count",
            },
        )

        self.assertTrue(result["exact"])
        self.assertEqual([item["value"] for item in result["comparisons"]], [125, 126])

    def test_results_include_shared_verification_and_safe_provenance(self):
        result = self.execute("aggregate_data", {
            "file_id": self.file_id,
            "worksheet_id": "sheet-1",
            "aggregation": "count",
        })
        self.assertEqual(result["operation"], "aggregate_data")
        self.assertEqual(result["operation_version"], 1)
        self.assertEqual(result["verification_status"], "exact")
        self.assertEqual(result["source_identities"][0]["file_id"], self.file_id)
        self.assertEqual(result["provenance"]["source_tables"], ["sheet-1"])
        self.assertNotIn("private-", json.dumps(result))

    def test_join_discovery_uses_safe_key_evidence_and_marks_many_to_many(self):
        second_id = "workbook-2"
        workbook = json.loads(self.chat_file["workbook_json"])
        # Rename Department to a shared key in both files and make both sides non-unique.
        left = workbook["worksheets"][0]
        left["columns"][0]["display_name"] = "Customer ID"
        left["columns"][0]["policy_key"] = "Customer ID"
        left["rows"][0]["values"][0] = None
        right = deepcopy(workbook)
        right["file_id"] = second_id
        right_sheet = right["worksheets"][0]
        right_sheet["columns"][0]["display_name"] = "Customer ID"
        right_sheet["columns"][0]["policy_key"] = "Customer ID"
        for row in right_sheet["rows"]:
            row["values"][0] = "Sales" if row["source_row"] % 2 else "Support"
            row["values"][1] = "memo" if row["source_row"] % 2 else "note"
        left_policy = json.loads(self.chat_file["privacy_policy_json"])["columns"]
        left_policy["Customer ID"] = left_policy.pop("Department")
        right_policy = deepcopy(left_policy)
        common = {**self.chat_file, "workbook_json": serialize_workbook(workbook),
                  "privacy_policy_json": policy_json(left_policy)}
        second = {**common, "file_id": second_id, "workbook_json": serialize_workbook(right),
                  "privacy_policy_json": policy_json(right_policy)}
        context = ToolContext(user_id="user-1", chat_id=self.chat_id,
                              files={self.file_id: common, second_id: second})
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={self.file_id: 1.0, second_id: 1.0}
        ):
            result = execute_tool(context, "discover_joins", {"file_ids": [self.file_id, second_id]})
        self.assertTrue(result["candidates"])
        candidate = result["candidates"][0]
        self.assertEqual(candidate["observed_cardinality"], "many_to_many")
        self.assertEqual(candidate["left_null_count"], 1)
        self.assertEqual(candidate["right_null_count"], 0)
        self.assertFalse(candidate["execution_eligible"])
        self.assertFalse(candidate["requires_confirmation"])
        self.assertNotIn("Sales", json.dumps(candidate))
        self.assertNotIn(self.column_ids["Amount"], {item["right"]["column_id"] for item in result["candidates"]})
        registered = {item["name"] for item in provider_tool_schemas()}
        self.assertIn("discover_joins", registered)
        self.assertIn("join_tables", registered)
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={self.file_id: 1.0, second_id: 1.0}
        ), self.assertRaises(ToolExecutionError) as unauthorized:
            execute_tool(context, "discover_joins", {"file_ids": [self.file_id, "not-attached"]})
        self.assertEqual(unauthorized.exception.code, "file_not_found")

    def test_join_execution_checks_cardinality_and_returns_bounded_provenance(self):
        left_id, right_id = "left-file", "right-file"
        original = json.loads(self.chat_file["workbook_json"])

        def make_file(file_id, keys, amount_name="Amount"):
            document = deepcopy(original)
            document["file_id"] = file_id
            sheet = document["worksheets"][0]
            sheet["columns"][1]["display_name"] = amount_name
            sheet["rows"] = sheet["rows"][:len(keys)]
            sheet["row_count"] = len(keys)
            for row, key in zip(sheet["rows"], keys):
                row["values"][0] = key
            return {**self.chat_file, "file_id": file_id, "row_count": len(keys),
                    "workbook_json": serialize_workbook(document)}

        left = make_file(left_id, ["K0", "K1", "K2", "K3"])
        right = make_file(right_id, ["K0", "K0", "K2", "K3"], "Revenue")
        context = ToolContext(user_id="user-1", chat_id=self.chat_id,
                              files={left_id: left, right_id: right})
        arguments = {
            "left_file_id": left_id, "left_worksheet_id": "sheet-1",
            "left_key_column_id": self.column_ids["Department"],
            "right_file_id": right_id, "right_worksheet_id": "sheet-1",
            "right_key_column_id": self.column_ids["Department"],
            "left_columns": [self.column_ids["Department"], self.column_ids["Amount"]],
            "right_columns": [self.column_ids["Department"], self.column_ids["Amount"]],
        }
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={left_id: 1.0, right_id: 1.0}
        ):
            result = execute_tool(context, "join_tables", arguments)
        self.assertTrue(result["exact"])
        self.assertEqual(result["cardinality"], "one_to_many")
        self.assertEqual(result["matched_row_count"], 4)
        self.assertEqual(len(result["rows"]), 4)
        self.assertEqual({item["file_id"] for item in result["source_identities"]}, {left_id, right_id})

        limited = ToolContext(user_id="user-1", chat_id=self.chat_id,
                              files={left_id: left, right_id: right})
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={left_id: 1.0, right_id: 1.0}
        ), patch("app.tools.MAX_JOIN_OUTPUT_ROWS", 2), self.assertRaises(ToolExecutionError) as too_large:
            execute_tool(limited, "join_tables", arguments)
        self.assertEqual(too_large.exception.code, "join_output_too_large")

        incompatible = ToolContext(
            user_id="user-1", chat_id=self.chat_id,
            files={left_id: left, right_id: make_file(right_id, [1, 2, 3, 4], "Revenue")},
        )
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={left_id: 1.0, right_id: 1.0}
        ), self.assertRaises(ToolExecutionError) as wrong_type:
            execute_tool(incompatible, "join_tables", arguments)
        self.assertEqual(wrong_type.exception.code, "incompatible_join_keys")

        unauthorized = ToolContext(user_id="user-1", chat_id=self.chat_id,
                                   files={left_id: left})
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={left_id: 1.0}
        ), self.assertRaises(ToolExecutionError) as no_access:
            execute_tool(unauthorized, "join_tables", arguments)
        self.assertEqual(no_access.exception.code, "file_not_found")

        one_to_one = ToolContext(
            user_id="user-1", chat_id=self.chat_id,
            files={left_id: left, right_id: make_file(right_id, ["K0", "K1", "K2", "K3"], "Revenue")},
        )
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={left_id: 1.0, right_id: 1.0}
        ):
            unique_result = execute_tool(one_to_one, "join_tables", arguments)
        self.assertEqual(unique_result["cardinality"], "one_to_one")
        self.assertEqual(unique_result["matched_row_count"], 4)

        reverse_left = make_file(left_id, ["K0", "K0", "K2", "K3"], "Revenue")
        reverse_right = make_file(right_id, ["K0", "K1", "K2", "K3"])
        many_to_one = ToolContext(user_id="user-1", chat_id=self.chat_id,
                                  files={left_id: reverse_left, right_id: reverse_right})
        reversed_arguments = {
            "left_file_id": left_id, "left_worksheet_id": "sheet-1",
            "left_key_column_id": self.column_ids["Department"],
            "right_file_id": right_id, "right_worksheet_id": "sheet-1",
            "right_key_column_id": self.column_ids["Department"],
            "left_columns": [self.column_ids["Department"]],
            "right_columns": [self.column_ids["Department"]],
        }
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={left_id: 1.0, right_id: 1.0}
        ):
            many_to_one_result = execute_tool(many_to_one, "join_tables", reversed_arguments)
        self.assertEqual(many_to_one_result["cardinality"], "many_to_one")
        self.assertEqual(many_to_one_result["matched_row_count"], 4)

        complete_left = make_file(left_id, [f"K{i}" for i in range(251)])
        complete_right = make_file(right_id, [f"K{i}" for i in range(251)], "Revenue")
        complete_left_doc = json.loads(complete_left["workbook_json"])
        complete_right_doc = json.loads(complete_right["workbook_json"])
        for idx, row in enumerate(complete_left_doc["worksheets"][0]["rows"], start=1):
            row["values"][1] = str(idx)
        for idx, row in enumerate(complete_right_doc["worksheets"][0]["rows"], start=1):
            row["values"][1] = "1"
        complete_left["workbook_json"] = serialize_workbook(complete_left_doc)
        complete_right["workbook_json"] = serialize_workbook(complete_right_doc)
        complete_context = ToolContext(user_id="user-1", chat_id=self.chat_id,
                                       files={left_id: complete_left, right_id: complete_right})
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={left_id: 1.0, right_id: 1.0}
        ):
            complete_join = execute_tool(complete_context, "join_tables", arguments)
            total = execute_tool(complete_context, "aggregate_joined_data", {
                "result_ref": complete_join["result_ref"],
                "aggregation": "sum",
                "column_ref": f"left:{self.column_ids['Amount']}",
            })
        self.assertEqual(complete_join["returned_row_count"], 25)
        self.assertEqual(complete_join["matched_row_count"], 251)
        self.assertEqual(total["row_count"], 251)
        self.assertEqual(total["value"], "31626")
        self.assertEqual(total["source_identities"], complete_join["source_identities"])
        self.assertEqual(total["provenance"]["transformations"], ["join_tables", "aggregate_joined_data"])
        cleanup_tool_context(complete_context)
        self.assertFalse(complete_context.join_results)

        left_many = make_file(left_id, ["K0", "K0", "K2", "K2"])
        right_many = make_file(right_id, ["K0", "K0", "K2", "K2"], "Revenue")
        unsafe = ToolContext(user_id="user-1", chat_id=self.chat_id,
                             files={left_id: left_many, right_id: right_many})
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={left_id: 1.0, right_id: 1.0}
        ), self.assertRaises(ToolExecutionError) as rejected:
            execute_tool(unsafe, "join_tables", arguments)
        self.assertEqual(rejected.exception.code, "many_to_many_join")

        excluded_args = {**arguments,
                         "left_key_column_id": self.column_ids["Excluded"],
                         "right_key_column_id": self.column_ids["Excluded"]}
        excluded_context = ToolContext(user_id="user-1", chat_id=self.chat_id,
                                       files={left_id: left, right_id: right})
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={left_id: 1.0, right_id: 1.0}
        ), self.assertRaises(ToolExecutionError) as excluded:
            execute_tool(excluded_context, "join_tables", excluded_args)
        self.assertEqual(excluded.exception.code, "column_not_found")

    def test_ambiguous_join_requires_matching_user_approved_binding(self):
        left_id, right_id = "amb-left", "amb-right"
        original = json.loads(self.chat_file["workbook_json"])
        left_doc = deepcopy(original)
        left_doc["file_id"] = left_id
        right_doc = deepcopy(original)
        right_doc["file_id"] = right_id
        left_doc["worksheets"][0]["rows"] = left_doc["worksheets"][0]["rows"][:4]
        right_doc["worksheets"][0]["rows"] = right_doc["worksheets"][0]["rows"][:4]
        for document in (left_doc, right_doc):
            document["worksheets"][0]["row_count"] = 4
            for index, row in enumerate(document["worksheets"][0]["rows"], start=1):
                row["values"][1] = str(index)
        left = {**self.chat_file, "file_id": left_id, "workbook_json": serialize_workbook(left_doc)}
        right = {**self.chat_file, "file_id": right_id, "workbook_json": serialize_workbook(right_doc)}
        discovery_context = ToolContext(user_id="user-1", chat_id=self.chat_id,
                                        files={left_id: left, right_id: right})
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={left_id: 1.0, right_id: 1.0}
        ), patch("app.tools.store.create_join_confirmation", return_value="confirmation-1"):
            discovery = execute_tool(discovery_context, "discover_joins", {"file_ids": [left_id, right_id]})
        self.assertTrue(any(candidate["requires_confirmation"] for candidate in discovery["candidates"]))
        self.assertNotIn("confirmation-1", json.dumps(discovery))
        self.assertEqual(discovery_context.pending_join_confirmations[0]["confirmation_id"], "confirmation-1")

        context = ToolContext(user_id="user-1", chat_id=self.chat_id,
                              files={left_id: left, right_id: right},
                              approved_join_confirmation_id="confirmation-1")
        amount_id = self.column_ids["Amount"]
        arguments = {
            "left_file_id": left_id, "left_worksheet_id": "sheet-1", "left_key_column_id": amount_id,
            "right_file_id": right_id, "right_worksheet_id": "sheet-1", "right_key_column_id": amount_id,
            "left_columns": [amount_id], "right_columns": [amount_id],
        }
        binding = {
            "left": {"file_id": left_id, "worksheet_id": "sheet-1", "column_id": amount_id},
            "right": {"file_id": right_id, "worksheet_id": "sheet-1", "column_id": amount_id},
            "source_identities": [
                _source_identity(context, left_id, "sheet-1"),
                _source_identity(context, right_id, "sheet-1"),
            ],
        }
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={left_id: 1.0, right_id: 1.0}
        ), patch("app.tools.store.consume_join_confirmation", return_value=binding) as consume:
            result = execute_tool(context, "join_tables", arguments)
        self.assertEqual(result["matched_row_count"], 4)
        consume.assert_called_once_with(
            chat_id=self.chat_id, user_id="user-1", confirmation_id="confirmation-1"
        )

        stale_context = ToolContext(user_id="user-1", chat_id=self.chat_id,
                                    files={left_id: left, right_id: right},
                                    approved_join_confirmation_id="confirmation-2")
        stale_binding = deepcopy(binding)
        stale_binding["source_identities"][1]["privacy_policy_revision"] = "stale-policy"
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={left_id: 1.0, right_id: 1.0}
        ), patch("app.tools.store.consume_join_confirmation", return_value=stale_binding), self.assertRaises(ToolExecutionError) as stale:
            execute_tool(stale_context, "join_tables", arguments)
        self.assertEqual(stale.exception.code, "invalid_confirmation")

        revision_context = ToolContext(user_id="user-1", chat_id=self.chat_id,
                                       files={left_id: left, right_id: right},
                                       approved_join_confirmation_id="confirmation-3")
        revision_binding = deepcopy(binding)
        revision_binding["source_identities"][0]["file_revision"] = "older-revision"
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={left_id: 1.0, right_id: 1.0}
        ), patch("app.tools.store.consume_join_confirmation", return_value=revision_binding), self.assertRaises(ToolExecutionError) as stale_revision:
            execute_tool(revision_context, "join_tables", arguments)
        self.assertEqual(stale_revision.exception.code, "invalid_confirmation")

    def test_multi_worksheet_schema_and_calculation_use_requested_sheet(self):
        workbook = json.loads(self.chat_file["workbook_json"])
        second = deepcopy(workbook["worksheets"][0])
        second["position"] = 1
        second["original_name"] = "Second"
        for index, column in enumerate(second["columns"]):
            column["policy_key"] = f"sheet-2::{column['display_name']}"
            column["column_id"] = column_identity(1, index, column["display_name"])
        workbook["worksheets"].append(second)
        workbook["worksheet_count"] = 2
        policy = json.loads(self.chat_file["privacy_policy_json"])["columns"]
        second_policy = {
            f"sheet-2::{name}": deepcopy(choice)
            for name, choice in list(policy.items())
        }
        policy.update(second_policy)
        file_copy = dict(self.chat_file)
        file_copy["workbook_json"] = serialize_workbook(workbook)
        file_copy["privacy_policy_json"] = policy_json(policy)
        self.chat_file = file_copy
        self.column_ids["second_amount"] = column_identity(1, 1, "Amount")

        schema = self.execute("get_workbook_schema", {"file_id": self.file_id})
        result = self.execute(
            "aggregate_data",
            {
                "file_id": self.file_id,
                "worksheet_id": "sheet-2",
                "aggregation": "count",
                "column_id": self.column_ids["second_amount"],
            },
        )

        self.assertEqual(len(schema["worksheets"]), 2)
        self.assertEqual(result["row_count"], 251)
        self.assertEqual(result["value"], 251)

    def test_date_min_max_and_invalid_mixed_numeric_data_are_explicit(self):
        workbook = json.loads(self.chat_file["workbook_json"])
        amount_column = workbook["worksheets"][0]["columns"][1]
        for index, row in enumerate(workbook["worksheets"][0]["rows"]):
            row["values"][1] = "2024-01-01" if index == 0 else "2024-01-02"
        file_copy = dict(self.chat_file)
        file_copy["workbook_json"] = serialize_workbook(workbook)
        self.chat_file = file_copy

        date_result = self.execute(
            "aggregate_data",
            {
                "file_id": self.file_id,
                "worksheet_id": "sheet-1",
                "aggregation": "minimum",
                "column_id": amount_column["column_id"],
            },
        )
        self.assertEqual(date_result["value"], "2024-01-01")
        self.assertEqual(date_result["result_type"], "date")

        workbook = json.loads(self.chat_file["workbook_json"])
        workbook["worksheets"][0]["rows"][1]["values"][1] = "not-a-number"
        file_copy["workbook_json"] = serialize_workbook(workbook)
        self.chat_file = file_copy
        with self.assertRaises(ToolExecutionError) as invalid:
            self.execute(
                "aggregate_data",
                {
                    "file_id": self.file_id,
                    "worksheet_id": "sheet-1",
                    "aggregation": "sum",
                    "column_id": amount_column["column_id"],
                },
            )
        self.assertEqual(invalid.exception.code, "non_numeric_column")

    def test_tool_execution_rechecks_chat_and_file_authorization(self):
        context = ToolContext(
            user_id="user-1",
            chat_id=self.chat_id,
            files={self.file_id: self.chat_file},
        )
        with patch("app.tools.store.get_chat", return_value=None):
            with self.assertRaises(ToolExecutionError) as denied:
                execute_tool(context, "get_workbook_schema", {"file_id": self.file_id})
        self.assertEqual(denied.exception.code, "chat_not_found")

        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions",
            return_value={},
        ):
            with self.assertRaises(ToolExecutionError) as missing:
                execute_tool(context, "get_workbook_schema", {"file_id": self.file_id})
        self.assertEqual(missing.exception.code, "file_not_found")

    def test_share_confirmation_result_size_and_call_limits_fail_closed(self):
        valid_file = self.chat_file
        workbook = json.loads(self.chat_file["workbook_json"])
        workbook["worksheets"][0]["columns"][2]["privacy"] = {
            "action": "share",
            "sensitive": True,
            "share_confirmed": False,
        }
        policy = json.loads(self.chat_file["privacy_policy_json"])["columns"]
        policy["Person"] = {
            "action": "share",
            "sensitive": True,
            "share_confirmed": False,
        }
        self.chat_file = {
            **self.chat_file,
            "workbook_json": serialize_workbook(workbook),
            "privacy_policy_json": policy_json(policy),
        }
        with self.assertRaises(ToolExecutionError) as unconfirmed:
            self.execute("get_workbook_schema", {"file_id": self.file_id})
        self.assertEqual(unconfirmed.exception.code, "privacy_validation_failed")

        self.chat_file = valid_file
        with patch("app.tools.MAX_TOOL_RESULT_BYTES", 10):
            with self.assertRaises(ToolExecutionError) as oversized:
                self.execute("get_workbook_schema", {"file_id": self.file_id})
        self.assertEqual(oversized.exception.code, "result_too_large")

        context = ToolContext(
            user_id="user-1",
            chat_id=self.chat_id,
            files={self.file_id: self.chat_file},
        )
        with patch("app.tools.MAX_TOOL_CALLS_PER_REQUEST", 1), patch(
            "app.tools.store.get_chat",
            return_value={"chat_id": self.chat_id},
        ), patch(
            "app.tools.store.get_chat_file_revisions",
            return_value={self.file_id: 1.0},
        ):
            execute_tool(context, "get_workbook_schema", {"file_id": self.file_id})
            with self.assertRaises(ToolExecutionError) as call_limit:
                execute_tool(context, "get_workbook_schema", {"file_id": self.file_id})
        self.assertEqual(call_limit.exception.code, "tool_call_limit")


if __name__ == "__main__":
    unittest.main()
