"""Regression tests for complete, policy-aware spreadsheet ingestion."""

import io
import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi import HTTPException
from openpyxl import Workbook

from app.privacy_policy import validate_policy
from app.workbook import (
    build_workbook_document,
    parse_workbook,
    workbook_provider_context,
)


class WorkbookIngestionTests(unittest.TestCase):
    def test_csv_keeps_leading_zeroes_and_normalizes_blank_duplicate_headers(self):
        parsed = parse_workbook("records.csv", b"Code,Code,\n001,002,active\n")
        sheet = parsed.worksheets[0]

        self.assertEqual(sheet.original_headers, ["Code", "Code", ""])
        self.assertEqual(sheet.headers, ["Code", "Code (2)", "Column 3"])
        self.assertEqual(sheet.dataframe.iloc[0].tolist(), ["001", "002", "active"])
        self.assertEqual(sheet.source_rows, [2])

    def test_xlsx_preserves_sheet_order_visibility_formulas_and_empty_sheets(self):
        source = Workbook()
        first = source.active
        first.title = "Summary"
        first.append(["Reference", "Formula"])
        first.append(["00017", "=1+2"])
        hidden = source.create_sheet("Private")
        hidden.sheet_state = "hidden"
        hidden.append(["Value"])
        hidden.append(["visible-to-policy-review"])
        source.create_sheet("Empty")
        stream = io.BytesIO()
        source.save(stream)

        parsed = parse_workbook("records.xlsx", stream.getvalue())

        self.assertEqual([sheet.name for sheet in parsed.worksheets], ["Summary", "Private", "Empty"])
        self.assertEqual(parsed.worksheets[0].dataframe.iloc[0].tolist(), ["00017", "=1+2"])
        self.assertEqual(parsed.worksheets[1].state, "hidden")
        self.assertEqual(parsed.worksheets[2].headers, [])
        policy = {
            "sheet-1::Reference": {"action": "share", "sensitive": False, "share_confirmed": False},
            "sheet-1::Formula": {"action": "share", "sensitive": False, "share_confirmed": False},
            "sheet-2::Value": {"action": "share", "sensitive": False, "share_confirmed": False},
        }
        types = {
            "sheet-1": {"Reference": None, "Formula": None},
            "sheet-2": {"Value": None},
            "sheet-3": {},
        }
        document = build_workbook_document(
            parsed,
            "file-id",
            policy,
            types,
            {f"sheet-{index + 1}": sheet.dataframe for index, sheet in enumerate(parsed.worksheets)},
        )
        self.assertEqual(document["worksheet_count"], 3)
        self.assertEqual(document["worksheets"][0]["rows"][0]["source_row"], 2)
        self.assertEqual(document["worksheets"][0]["rows"][0]["values"][0], "00017")

    def test_provider_projection_excludes_columns_and_checks_mask_and_share(self):
        workbook = {
            "schema_version": 1,
            "worksheets": [{
                "position": 0,
                "row_count": 1,
                "column_count": 3,
                "columns": [
                    {"column_id": "c1", "policy_key": "Email", "display_name": "Email", "position": 0, "data_type": "EMAIL",
                     "privacy": {"action": "mask", "sensitive": True, "share_confirmed": False}},
                    {"column_id": "c2", "policy_key": "Salary", "display_name": "Salary", "position": 1,
                     "privacy": {"action": "share", "sensitive": False, "share_confirmed": False}},
                    {"column_id": "c3", "policy_key": "Secret", "display_name": "Secret", "position": 2,
                     "privacy": {"action": "exclude", "sensitive": False, "share_confirmed": False}},
                ],
                "rows": [{"source_row": 2, "values": [
                    "EMAIL_PERSON_1",
                    125000,
                    "excluded-value-must-not-leak",
                ]}],
            }],
        }
        policy = {
            "Email": {"action": "mask", "sensitive": True, "share_confirmed": False},
            "Salary": {"action": "share", "sensitive": False, "share_confirmed": False},
            "Secret": {"action": "exclude", "sensitive": False, "share_confirmed": False},
        }

        output = workbook_provider_context(workbook, policy)

        self.assertIn("EMAIL_PERSON_1", output)
        self.assertIn("125000", output)
        self.assertIn("__privy_source_row__", output)
        self.assertNotIn("excluded-value-must-not-leak", output)
        self.assertNotIn("Secret", output)

    def test_provider_projection_fails_closed_for_mask_leak_and_unconfirmed_share(self):
        workbook = {
            "worksheets": [{
                "position": 0,
                "row_count": 1,
                "column_count": 1,
                "columns": [{
                    "column_id": "c1",
                    "policy_key": "Email",
                    "display_name": "Email",
                    "position": 0,
                    "data_type": "EMAIL",
                    "privacy": {"action": "mask", "sensitive": True, "share_confirmed": False},
                }],
                "rows": [{"source_row": 2, "values": ["alice@example.test"]}],
            }],
        }
        policy = {
            "Email": {"action": "mask", "sensitive": True, "share_confirmed": False},
        }
        with self.assertRaises(HTTPException) as leak:
            workbook_provider_context(workbook, policy)
        self.assertEqual(leak.exception.status_code, 422)

        workbook["worksheets"][0]["columns"][0]["privacy"]["action"] = "share"
        policy["Email"] = {"action": "share", "sensitive": True, "share_confirmed": False}
        with self.assertRaises(HTTPException) as confirmation:
            workbook_provider_context(workbook, policy)
        self.assertEqual(confirmation.exception.status_code, 422)

    def test_policy_rejects_missing_duplicate_column_choices(self):
        with self.assertRaises(HTTPException) as mismatch:
            validate_policy(
                ["sheet-1::Value", "sheet-2::Value"],
                {"sheet-1::Value": None, "sheet-2::Value": None},
                {"sheet-1::Value": {"action": "share"}},
            )
        self.assertEqual(mismatch.exception.status_code, 422)

    def test_row_limit_is_rejected_without_truncating_data(self):
        with patch("app.workbook.MAX_ROWS_PER_FILE", 1):
            with self.assertRaises(HTTPException) as limit:
                parse_workbook("records.csv", b"Code\none\ntwo\n")
        self.assertEqual(limit.exception.status_code, 413)

    def test_cell_limit_is_rejected_and_large_preview_does_not_truncate_storage(self):
        with patch("app.workbook.MAX_CELLS_PER_FILE", 3):
            with self.assertRaises(HTTPException) as limit:
                parse_workbook("records.csv", b"A,B\n1,2\n")
        self.assertEqual(limit.exception.status_code, 413)

        payload = "Reference\n" + "".join(f"{row:05}\n" for row in range(250))
        parsed = parse_workbook("records.csv", payload.encode())
        self.assertEqual(len(parsed.worksheets[0].dataframe), 250)
        self.assertEqual(parsed.worksheets[0].dataframe.iloc[0, 0], "00000")
        self.assertEqual(parsed.worksheets[0].dataframe.iloc[249, 0], "00249")

    def test_unsupported_encoding_and_file_type_are_rejected(self):
        with self.assertRaises(HTTPException):
            parse_workbook("records.csv", b"Name\n\xff\n")
        with self.assertRaises(HTTPException):
            parse_workbook("records.txt", b"data")
        with self.assertRaises(HTTPException):
            parse_workbook("broken.xlsx", b"not a workbook")


if __name__ == "__main__":
    unittest.main()
