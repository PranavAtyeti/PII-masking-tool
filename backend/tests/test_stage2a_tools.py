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
from app.tools import ToolContext, ToolExecutionError, execute_tool
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
