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
from app.tools import ToolContext, ToolExecutionError, _source_identity, cleanup_tool_context, execute_tool, provider_tool_schemas, tool_capabilities
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

    def make_csv_chat_file(self, file_id, csv_text):
        parsed = parse_workbook("synthetic.csv", csv_text.encode())
        worksheet = parsed.worksheets[0]
        names = list(worksheet.dataframe.columns)
        policy = {name: {"action": "share", "sensitive": False, "share_confirmed": False} for name in names}
        types = {"sheet-1": {name: None for name in names}}
        document = build_workbook_document(parsed, file_id, policy, types, {"sheet-1": worksheet.dataframe.copy()})
        return {
            "file_id": file_id, "updated_at": 1.0, "workbook_json": serialize_workbook(document),
            "privacy_policy_json": policy_json(policy),
            "columns_json": json.dumps([{"name": name, "type": None} for name in names]),
            "masked_csv": "", "row_count": len(worksheet.dataframe),
        }

    def ids_for(self, *names):
        return {name: column_identity(0, index, name) for index, name in enumerate(names)}

    def test_registered_plan_chains_three_sources_and_preserves_provenance(self):
        ids = ["plan-left", "plan-middle", "plan-right"]
        original = json.loads(self.chat_file["workbook_json"])

        def make_file(file_id, amount):
            document = deepcopy(original)
            document["file_id"] = file_id
            sheet = document["worksheets"][0]
            sheet["rows"] = sheet["rows"][:4]
            sheet["row_count"] = 4
            for index, row in enumerate(sheet["rows"]):
                row["values"][0] = f"K{index}"
                row["values"][1] = str(amount)
            return {**self.chat_file, "file_id": file_id, "row_count": 4,
                    "workbook_json": serialize_workbook(document)}

        context = ToolContext(user_id="user-1", chat_id=self.chat_id,
                              files={ids[0]: make_file(ids[0], 2), ids[1]: make_file(ids[1], 3), ids[2]: make_file(ids[2], 5)},
                              approved_join_confirmation_id="approved-1")
        dept, amount = self.column_ids["Department"], self.column_ids["Amount"]
        plan = {
            "objective": "Sum the third source amount after matching all three tables by Department.",
            "steps": [
                {"step_id": "join_ab", "capability": "join_tables", "arguments": {
                    "left_file_id": ids[0], "left_worksheet_id": "sheet-1", "left_key_column_id": dept,
                    "right_file_id": ids[1], "right_worksheet_id": "sheet-1", "right_key_column_id": dept,
                    "left_columns": [dept], "right_columns": [amount],
                }},
                {"step_id": "join_abc", "capability": "join_tables", "depends_on": ["join_ab"], "arguments": {
                    "left_result_ref": {"$ref": "join_ab.result_ref"}, "left_key_column_ref": f"left:{dept}",
                    "right_file_id": ids[2], "right_worksheet_id": "sheet-1", "right_key_column_id": dept,
                    "left_columns": [f"left:{dept}", f"right:{amount}"], "right_columns": [amount],
                }},
                {"step_id": "total", "capability": "aggregate_joined_data", "depends_on": ["join_abc"], "arguments": {
                    "result_ref": {"$ref": "join_abc.result_ref"}, "aggregation": "sum",
                    "column_ref": f"right:{ids[2]}:{amount}",
                }},
                {"step_id": "groups", "capability": "group_and_aggregate", "depends_on": ["join_abc"], "arguments": {
                    "result_ref": {"$ref": "join_abc.result_ref"},
                    "group_column_ids": [f"left:left:{dept}", f"right:{ids[2]}:{amount}"],
                    "aggregation": "sum", "metric_column_id": f"right:{ids[2]}:{amount}",
                    "sort_direction": "descending", "limit": 4,
                }},
            ],
        }
        revisions = {file_id: 1.0 for file_id in ids}
        binding = {
            "left": {"file_id": ids[0], "worksheet_id": "sheet-1", "column_id": dept},
            "right": {"file_id": ids[1], "worksheet_id": "sheet-1", "column_id": dept},
            "source_identities": [_source_identity(context, ids[0], "sheet-1"), _source_identity(context, ids[1], "sheet-1")],
        }
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value=revisions
        ), patch("app.tools.store.consume_join_confirmation", return_value=binding):
            result = execute_tool(context, "execute_analysis_plan", plan)
        self.assertTrue(result["exact"], result)
        self.assertFalse(result["partial_result"])
        self.assertEqual(result["completed_step_count"], 4)
        self.assertEqual(result["steps"][2]["result"]["value"], "20")
        self.assertEqual({item["file_id"] for item in result["source_identities"]}, set(ids))
        self.assertEqual(result["steps"][2]["result"]["provenance"]["transformations"],
                         ["join_tables", "join_tables", "aggregate_joined_data"])
        self.assertEqual(result["steps"][3]["result"]["group_count"], 4)
        self.assertEqual(len(result["steps"][3]["result"]["source_identities"]), 3)
        self.assertLessEqual(context.join_result_bytes, 32 * 1024 * 1024)
        cleanup_tool_context(context)
        self.assertFalse(context.join_results)

    def test_plan_rejects_unknown_capability_and_keeps_unrelated_partial_success(self):
        context = ToolContext(user_id="user-1", chat_id=self.chat_id, files={self.file_id: self.chat_file})
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={self.file_id: 1.0}
        ):
            with self.assertRaises(ToolExecutionError) as unknown:
                execute_tool(context, "execute_analysis_plan", {"objective": "x", "steps": [
                    {"step_id": "bad", "capability": "run_python", "arguments": {}}
                ]})
            self.assertEqual(unknown.exception.code, "unknown_capability")
            partial = execute_tool(context, "execute_analysis_plan", {"objective": "x", "steps": [
                {"step_id": "bad", "capability": "aggregate_data", "arguments": {
                    "file_id": self.file_id, "worksheet_id": "sheet-1", "aggregation": "median",
                }},
                {"step_id": "schema", "capability": "get_workbook_schema", "arguments": {"file_id": self.file_id}},
            ]})
        self.assertTrue(partial["partial_result"])
        self.assertEqual(partial["steps"][0]["status"], "failed")
        self.assertEqual(partial["steps"][1]["status"], "completed")
        self.assertIn("execute_analysis_plan", {item["name"] for item in tool_capabilities()})
        self.assertIn("aggregate_joined_data", {item["name"] for item in tool_capabilities()})

    def test_plan_pauses_when_join_discovery_needs_user_confirmation(self):
        left_id, right_id = "pause-left", "pause-right"
        original = json.loads(self.chat_file["workbook_json"])

        def make_file(file_id):
            document = deepcopy(original)
            document["file_id"] = file_id
            sheet = document["worksheets"][0]
            sheet["rows"] = sheet["rows"][:4]
            sheet["row_count"] = 4
            for index, row in enumerate(sheet["rows"]):
                row["values"][0] = f"K{index}"
                row["values"][1] = str(index + 1)
            return {**self.chat_file, "file_id": file_id, "row_count": 4,
                    "workbook_json": serialize_workbook(document)}

        context = ToolContext(user_id="user-1", chat_id=self.chat_id,
                              files={left_id: make_file(left_id), right_id: make_file(right_id)})
        plan = {"objective": "Find and then execute the relationship.", "steps": [
            {"step_id": "discover", "capability": "discover_joins", "arguments": {"file_ids": [left_id, right_id]}},
            {"step_id": "join", "capability": "join_tables", "depends_on": ["discover"], "arguments": {
                "left_file_id": left_id, "left_worksheet_id": "sheet-1", "left_key_column_id": self.column_ids["Department"],
                "right_file_id": right_id, "right_worksheet_id": "sheet-1", "right_key_column_id": self.column_ids["Department"],
                "left_columns": [self.column_ids["Department"]], "right_columns": [self.column_ids["Amount"]],
            }},
        ]}
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={left_id: 1.0, right_id: 1.0}
        ), patch("app.tools.store.create_join_confirmation", side_effect=["confirm-1", "confirm-2"]):
            result = execute_tool(context, "execute_analysis_plan", plan)
        self.assertTrue(result["awaiting_confirmation"])
        self.assertFalse(result["exact"])
        self.assertEqual(result["steps"][0]["status"], "awaiting_confirmation")
        self.assertEqual(result["steps"][1]["status"], "paused_for_confirmation")
        self.assertTrue(context.pending_join_confirmations)

    def test_plan_rejects_forward_dependencies_and_undeclared_references(self):
        context = ToolContext(user_id="user-1", chat_id=self.chat_id, files={self.file_id: self.chat_file})
        revisions = {self.file_id: 1.0}
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value=revisions
        ):
            with self.assertRaises(ToolExecutionError) as forward:
                execute_tool(context, "execute_analysis_plan", {"objective": "x", "steps": [
                    {"step_id": "first", "capability": "get_workbook_schema", "depends_on": ["second"], "arguments": {"file_id": self.file_id}},
                    {"step_id": "second", "capability": "get_workbook_schema", "arguments": {"file_id": self.file_id}},
                ]})
            self.assertEqual(forward.exception.code, "invalid_plan")
            with self.assertRaises(ToolExecutionError) as reference:
                execute_tool(context, "execute_analysis_plan", {"objective": "x", "steps": [
                    {"step_id": "first", "capability": "aggregate_joined_data", "arguments": {
                        "result_ref": {"$ref": "other.result_ref"}, "aggregation": "count",
                    }},
                ]})
            self.assertEqual(reference.exception.code, "invalid_plan_reference")

    def test_dataset_profile_reports_quality_statistics_without_raw_values(self):
        file_id = "profile-fixture"
        chat_file = self.make_csv_chat_file(file_id, "Department,Amount,Code\nA,1.00,X\nA,2.00,X\nB,3.00,Y\nB,1000.00,\n")
        context = ToolContext(user_id="user-1", chat_id=self.chat_id, files={file_id: chat_file})
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={file_id: 1.0}
        ):
            result = execute_tool(context, "profile_dataset", {"file_id": file_id, "worksheet_id": "sheet-1"})
        self.assertEqual(result["row_count"], 4)
        self.assertEqual(result["column_count"], 3)
        self.assertEqual(result["duplicate_row_count"], 0)
        profile = {column["name"]: column for column in result["columns"]}
        self.assertEqual(profile["Amount"]["missing_count"], 0)
        self.assertEqual(Decimal(profile["Amount"]["mean"]), Decimal("251.5"))
        self.assertEqual(Decimal(profile["Amount"]["median"]), Decimal("2.5"))
        self.assertEqual(profile["Amount"]["outlier_method"], "IQR_1.5")
        self.assertEqual(profile["Amount"]["outlier_count"], 1)
        self.assertEqual(profile["Code"]["missing_count"], 1)
        self.assertEqual(profile["Code"]["duplicate_value_count"], 1)
        self.assertNotIn('"value": "X"', json.dumps(result))

    def test_filter_sort_then_rank_multiple_group_aggregates_in_one_plan(self):
        file_id = "composable-fixture"
        chat_file = self.make_csv_chat_file(file_id, "Department,Amount\nA,10\nB,20\nA,30\nB,40\nA,50\nB,60\n")
        context = ToolContext(user_id="user-1", chat_id=self.chat_id, files={file_id: chat_file})
        columns = self.ids_for("Department", "Amount")
        plan = {"objective": "Filter large rows, then rank departments by total and average amount.", "steps": [
            {"step_id": "filtered", "capability": "filter_rows", "arguments": {
                "file_id": file_id, "worksheet_id": "sheet-1", "conditions": [
                    {"column_id": columns["Amount"], "operator": "gte", "value": 20},
                ], "columns": [columns["Department"], columns["Amount"]],
                "sort_by": [{"column_id": columns["Amount"], "direction": "descending"}],
            }},
            {"step_id": "ranked", "capability": "group_and_aggregate", "depends_on": ["filtered"], "arguments": {
                "result_ref": {"$ref": "filtered.result_ref"}, "group_column_id": columns["Department"],
                "aggregation": "sum", "metric_column_id": columns["Amount"],
                "additional_aggregations": [{"alias": "avg", "aggregation": "average", "metric_column_id": columns["Amount"]}],
                "sort_direction": "descending", "rank_method": "dense", "limit": 2,
            }},
        ]}
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={file_id: 1.0}
        ):
            result = execute_tool(context, "execute_analysis_plan", plan)
        ranked = result["steps"][1]["result"]["groups"]
        self.assertEqual([item["group"] for item in ranked], ["B", "A"])
        self.assertEqual([item["value"] for item in ranked], ["120", "80"])
        self.assertEqual([item["aggregate_values"]["avg"]["value"] for item in ranked], ["40", "40"])
        self.assertEqual([item["rank"] for item in ranked], [1, 2])
        self.assertFalse(result["partial_result"])
        cleanup_tool_context(context)

    def test_trends_are_chronological_and_report_invalid_dates(self):
        file_id = "trend-fixture"
        chat_file = self.make_csv_chat_file(file_id, "Date,Amount\n2024-02-03,3\n2024-01-20,2.75\n2024-01-02,1.25\nbad-date,4\n,5\n")
        context = ToolContext(user_id="user-1", chat_id=self.chat_id, files={file_id: chat_file})
        columns = self.ids_for("Date", "Amount")
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={file_id: 1.0}
        ):
            result = execute_tool(context, "trend_analysis", {
                "file_id": file_id, "worksheet_id": "sheet-1", "date_column_id": columns["Date"],
                "interval": "month", "aggregation": "sum", "metric_column_id": columns["Amount"],
            })
        self.assertEqual([group["period"] for group in result["groups"]], ["2024-01-01", "2024-02-01"])
        self.assertEqual([group["value"] for group in result["groups"]], ["4.00", "3"])
        self.assertEqual(result["invalid_date_count"], 1)
        self.assertEqual(result["missing_date_count"], 1)

    def test_derived_column_uses_fixed_operations_and_feeds_exact_aggregate(self):
        file_id = "calc-fixture"
        chat_file = self.make_csv_chat_file(file_id, "Revenue,Cost\n100,50\n80,0\n60,30\n")
        context = ToolContext(user_id="user-1", chat_id=self.chat_id, files={file_id: chat_file})
        columns = self.ids_for("Revenue", "Cost")
        plan = {"objective": "Calculate margin and sum it.", "steps": [
            {"step_id": "margin", "capability": "calculate_columns", "arguments": {
                "file_id": file_id, "worksheet_id": "sheet-1", "operation": "subtract",
                "left_column_id": columns["Revenue"], "right_column_id": columns["Cost"], "alias": "margin",
            }},
            {"step_id": "total", "capability": "aggregate_joined_data", "depends_on": ["margin"], "arguments": {
                "result_ref": {"$ref": "margin.result_ref"}, "aggregation": "sum", "column_ref": "derived:margin",
            }},
        ]}
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={file_id: 1.0}
        ):
            result = execute_tool(context, "execute_analysis_plan", plan)
        self.assertEqual(result["steps"][1]["result"]["value"], "160")
        self.assertEqual({item["file_id"] for item in result["source_identities"]}, {file_id})
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={file_id: 1.0}
        ):
            division = execute_tool(context, "calculate_columns", {
                "file_id": file_id, "worksheet_id": "sheet-1", "operation": "divide",
                "left_column_id": columns["Revenue"], "right_column_id": columns["Cost"], "alias": "ratio",
            })
        self.assertEqual(division["division_by_zero_count"], 1)
        cleanup_tool_context(context)

    def test_compare_reports_absolute_and_percentage_change_with_zero_baseline(self):
        file_id = "compare-fixture"
        chat_file = self.make_csv_chat_file(file_id, "Department,Amount\nA,0\nB,10\nA,0\nB,20\n")
        context = ToolContext(user_id="user-1", chat_id=self.chat_id, files={file_id: chat_file})
        columns = self.ids_for("Department", "Amount")
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={file_id: 1.0}
        ):
            result = execute_tool(context, "compare_groups", {
                "file_id": file_id, "worksheet_id": "sheet-1", "group_column_id": columns["Department"],
                "group_values": ["A", "B"], "aggregation": "sum", "metric_column_id": columns["Amount"],
                "include_change": True,
            })
        self.assertEqual(result["absolute_difference"], "30")
        self.assertIsNone(result["percentage_change"])
        self.assertTrue(result["warnings"])

    def test_compare_consumes_derived_result_and_reports_percentage_rounding(self):
        file_id = "compare-derived-fixture"
        chat_file = self.make_csv_chat_file(file_id, "Department,Amount\nA,3\nB,4\n")
        context = ToolContext(user_id="user-1", chat_id=self.chat_id, files={file_id: chat_file})
        columns = self.ids_for("Department", "Amount")
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={file_id: 1.0}
        ):
            derived = execute_tool(context, "calculate_columns", {
                "file_id": file_id, "worksheet_id": "sheet-1", "operation": "add",
                "left_column_id": columns["Amount"], "right_column_id": columns["Amount"], "alias": "double_amount",
            })
            result = execute_tool(context, "compare_groups", {
                "result_ref": derived["result_ref"], "group_column_id": columns["Department"],
                "group_values": ["A", "B"], "aggregation": "sum", "metric_column_id": "derived:double_amount",
                "include_change": True,
            })
        self.assertEqual(result["absolute_difference"], "2")
        self.assertTrue(result["percentage_change"].startswith("33.333333333333333333333333"))
        self.assertFalse(result["exact"])
        self.assertEqual(result["comparison_precision"], "decimal_48_significant_digits")
        cleanup_tool_context(context)

    def test_derived_references_fail_closed_after_revision_or_policy_changes(self):
        file_id = "stale-derived-fixture"
        chat_file = self.make_csv_chat_file(file_id, "Revenue,Cost\n100,50\n")
        columns = self.ids_for("Revenue", "Cost")
        context = ToolContext(user_id="user-1", chat_id=self.chat_id, files={file_id: chat_file})
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={file_id: 1.0}
        ):
            derived = execute_tool(context, "calculate_columns", {
                "file_id": file_id, "worksheet_id": "sheet-1", "operation": "subtract",
                "left_column_id": columns["Revenue"], "right_column_id": columns["Cost"], "alias": "margin",
            })
        updated = {**chat_file, "updated_at": 2.0}
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={file_id: 2.0}
        ), patch("app.tools.store.get_chat_file", return_value=updated), self.assertRaises(ToolExecutionError) as stale_revision:
            execute_tool(context, "aggregate_joined_data", {
                "result_ref": derived["result_ref"], "aggregation": "sum", "column_ref": "derived:margin",
            })
        self.assertEqual(stale_revision.exception.code, "stale_result")

        context = ToolContext(user_id="user-1", chat_id=self.chat_id, files={file_id: chat_file})
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={file_id: 1.0}
        ):
            derived = execute_tool(context, "calculate_columns", {
                "file_id": file_id, "worksheet_id": "sheet-1", "operation": "subtract",
                "left_column_id": columns["Revenue"], "right_column_id": columns["Cost"], "alias": "margin",
            })
        updated_policy = {"Revenue": {"action": "mask", "sensitive": True, "share_confirmed": False},
                         "Cost": {"action": "share", "sensitive": False, "share_confirmed": False}}
        context.files[file_id]["privacy_policy_json"] = policy_json(updated_policy)
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={file_id: 1.0}
        ), self.assertRaises(ToolExecutionError) as stale_policy:
            execute_tool(context, "aggregate_joined_data", {
                "result_ref": derived["result_ref"], "aggregation": "sum", "column_ref": "derived:margin",
            })
        self.assertEqual(stale_policy.exception.code, "stale_result")

    def test_derived_results_obey_shared_intermediate_memory_ceiling(self):
        file_id = "memory-derived-fixture"
        chat_file = self.make_csv_chat_file(file_id, "Revenue,Cost\n100,50\n")
        columns = self.ids_for("Revenue", "Cost")
        context = ToolContext(user_id="user-1", chat_id=self.chat_id, files={file_id: chat_file})
        with patch("app.tools.store.get_chat", return_value={"chat_id": self.chat_id}), patch(
            "app.tools.store.get_chat_file_revisions", return_value={file_id: 1.0}
        ), patch("app.tools.MAX_JOIN_MEMORY_BYTES", 1), self.assertRaises(ToolExecutionError) as limited:
            execute_tool(context, "calculate_columns", {
                "file_id": file_id, "worksheet_id": "sheet-1", "operation": "subtract",
                "left_column_id": columns["Revenue"], "right_column_id": columns["Cost"], "alias": "margin",
            })
        self.assertEqual(limited.exception.code, "join_memory_limit")
        self.assertFalse(context.join_results)

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
