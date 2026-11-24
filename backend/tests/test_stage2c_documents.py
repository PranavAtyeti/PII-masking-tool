"""DOCX extraction, archive safety, and policy-aware retrieval tests."""

import io
import json
import os
import sys
import unittest
import zipfile
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("PRIVY_JWT_SECRET", "test-secret-for-docx-tests")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import docx_ingestion, document_retrieval, mapping_store, workbook as workbook_module
from app.crypto import generate_key, is_encrypted
from app.models import Chat, ChatFile, TokenEntry
from app.privacy_policy import validate_outbound_text
from app import tools
from app.routers.upload import (
    _document_policy,
    _document_sensitive_type,
    _masked_document,
    _read_docx,
)


CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
</Types>"""
DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:body>
  <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Quarterly Résumé</w:t></w:r></w:p>
  <w:p><w:r><w:t>Unicode café — team summary.</w:t></w:r></w:p>
  <w:p><w:r><w:t></w:t></w:r></w:p>
  <w:tbl>
   <w:tr><w:tc><w:p><w:r><w:t>Region</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Revenue</w:t></w:r></w:p></w:tc></w:tr>
   <w:tr><w:tc><w:p><w:r><w:t>West</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>120.50</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl>
 </w:body>
</w:document>"""


def make_docx(
    document: str = DOCUMENT,
    extra: dict[str, str] | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("word/document.xml", document)
        archive.writestr(
            "word/styles.xml",
            '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:style w:styleId="Heading1"><w:name w:val="heading 1"/></w:style></w:styles>',
        )
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return output.getvalue()


def stored_document(sections: list[dict], action="mask", sensitive=False, confirmed=False) -> dict:
    file_id = "doc-file-1"
    return {
        "file_id": file_id,
        "workbook_json": json.dumps({
            "schema_version": 1,
            "document_kind": "docx",
            "file_id": file_id,
            "sections": sections,
        }),
        "privacy_policy_json": json.dumps({
            "version": 1,
            "columns": {
                "document_content": {
                    "action": action,
                    "sensitive": sensitive,
                    "share_confirmed": confirmed,
                }
            },
        }),
        "columns_json": json.dumps([{
            "name": "document_content",
            "type": "PERSON" if sensitive else None,
            "enabled": action == "mask",
        }]),
    }


class DocxIngestionTests(unittest.TestCase):
    def test_infers_header_rows_but_preserves_data_first_table(self):
        data_first = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:body><w:tbl>
  <w:tr><w:tc><w:p><w:r><w:t>CUST-001</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Pune</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>CUST-002</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Mumbai</w:t></w:r></w:p></w:tc></w:tr>
 </w:tbl></w:body></w:document>"""
        parsed = docx_ingestion.parse_docx(make_docx(data_first), "doc-data-first")
        table_rows = [section for section in parsed["sections"] if section["kind"] == "table_row"]
        self.assertFalse(any(row["is_header"] for row in table_rows))

        with_header = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:body><w:tbl>
  <w:tr><w:tc><w:p><w:r><w:t>Customer ID</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>City</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>CUST-001</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Pune</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>Customer ID</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>City</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>CUST-002</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Mumbai</w:t></w:r></w:p></w:tc></w:tr>
 </w:tbl></w:body></w:document>"""
        parsed = docx_ingestion.parse_docx(make_docx(with_header), "doc-headers")
        table_rows = [section for section in parsed["sections"] if section["kind"] == "table_row"]
        self.assertEqual([row["is_header"] for row in table_rows], [True, False, True, False])

    def test_extracts_heading_paragraph_unicode_tables_and_order(self):
        parsed = docx_ingestion.parse_docx(make_docx(), "doc-1")

        self.assertEqual(parsed["document_kind"], "docx")
        self.assertEqual(parsed["extraction_status"], "complete")
        self.assertEqual(parsed["document_revision"], __import__("hashlib").sha256(make_docx()).hexdigest())
        self.assertEqual(parsed["sections"][0]["kind"], "heading")
        self.assertEqual(parsed["sections"][0]["heading_level"], 1)
        self.assertEqual(parsed["sections"][1]["content"], "Unicode café — team summary.")
        self.assertEqual(parsed["sections"][2]["kind"], "table_row")
        self.assertEqual(parsed["sections"][2]["source_id"], "table-0001-row-0001")
        self.assertEqual(parsed["sections"][3]["cells"][1]["content"], "120.50")
        self.assertEqual([item["order"] for item in parsed["sections"]], [1, 2, 3, 4])
        self.assertEqual(parsed["table_count"], 1)
        self.assertFalse(parsed["page_numbers_available"])

    def test_includes_headers_and_ignores_external_relationships(self):
        payload = make_docx(extra={
            "word/header1.xml": (
                '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:p><w:r><w:t>Header note</w:t></w:r></w:p></w:hdr>'
            ),
            "word/_rels/document.xml.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Target="https://invalid.example/" TargetMode="External"/>'
                '</Relationships>'
            ),
            "word/media/image1.png": "not extracted",
        })
        parsed = docx_ingestion.parse_docx(payload, "doc-1")
        self.assertIn("headers_footers_included", parsed["warnings"])
        self.assertIn("external_relationships_ignored", parsed["warnings"])
        self.assertIn("embedded_images_not_extracted", parsed["warnings"])
        self.assertTrue(any(section.get("content") == "Header note" for section in parsed["sections"]))

    def test_rejects_empty_malformed_unsafe_xml_and_non_docx_mime(self):
        for payload in (b"", b"not a zip"):
            with self.subTest(payload=payload), self.assertRaises(HTTPException):
                docx_ingestion.parse_docx(payload, "doc-1")

        with self.assertRaises(HTTPException):
            docx_ingestion.parse_docx(make_docx("<w:document"), "doc-1")
        with self.assertRaises(HTTPException):
            docx_ingestion.parse_docx(
                make_docx('<!DOCTYPE x [<!ENTITY leak "bad">]><w:document/>'),
                "doc-1",
            )
        utf16_dtd = (
            '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY leak "bad">]>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'
        ).encode("utf-16")
        with self.assertRaises(HTTPException):
            docx_ingestion.parse_docx(make_docx(utf16_dtd), "doc-1")
        with self.assertRaises(HTTPException):
            _read_docx("not-really.docx", "text/plain", make_docx(), "doc-1")

    def test_rejects_empty_text_duplicate_entries_and_unsafe_paths(self):
        empty = make_docx(
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>'
        )
        with self.assertRaises(HTTPException):
            docx_ingestion.parse_docx(empty, "doc-1")

        duplicate = io.BytesIO()
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr("word/document.xml", DOCUMENT)
            archive.writestr("word/document.xml", DOCUMENT)
        with self.assertRaises(HTTPException):
            docx_ingestion.parse_docx(duplicate.getvalue(), "doc-1")

        with self.assertRaises(HTTPException):
            docx_ingestion.parse_docx(
                make_docx(extra={"../outside.xml": "<unsafe/>"}),
                "doc-1",
            )

    def test_enforces_entry_ratio_and_expanded_size_limits(self):
        payload = make_docx()
        with patch.object(docx_ingestion, "DOCX_MAX_ZIP_ENTRIES", 1):
            with self.assertRaises(HTTPException) as error:
                docx_ingestion.parse_docx(payload, "doc-1")
            self.assertEqual(error.exception.status_code, 413)
        with patch.object(docx_ingestion, "DOCX_MAX_COMPRESSION_RATIO", 1):
            with self.assertRaises(HTTPException):
                docx_ingestion.parse_docx(payload, "doc-1")
        with patch.object(docx_ingestion, "DOCX_MAX_UNCOMPRESSED_BYTES", 100):
            with self.assertRaises(HTTPException) as error:
                docx_ingestion.parse_docx(payload, "doc-1")
            self.assertEqual(error.exception.status_code, 413)


class DocumentPrivacyAndRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.section = {
            "source_id": "p-000001",
            "kind": "paragraph",
            "order": 1,
            "heading_path": ["Contact details"],
            "content": "Contact alice@example.test about the project delivery.",
        }

    def test_sensitive_document_share_requires_confirmation_and_masking_reuses_mapper(self):
        with self.assertRaises(HTTPException):
            _document_policy(
                {"document_content": {"action": "share", "share_confirmed": False}},
                "EMAIL",
            )
        policy = _document_policy(
            {"document_content": {"action": "share", "share_confirmed": True}},
            "EMAIL",
        )
        self.assertTrue(policy["document_content"]["share_confirmed"])

        with patch(
            "app.routers.upload.mask_free_texts",
            return_value={self.section["content"]: "Contact [EMAIL_1] about the project delivery."},
        ) as masker, patch.object(mapping_store, "load_counters", return_value={}):
            document, count = _masked_document(
                {"sections": [self.section]},
                "mask",
                "chat-1",
                False,
                0.6,
            )
        self.assertEqual(document["sections"][0]["content"], "Contact [EMAIL_1] about the project delivery.")
        self.assertEqual(count, 1)
        masker.assert_called_once()

    def test_sensitivity_classification_samples_across_the_document(self):
        sections = [
            {"kind": "paragraph", "content": f"Ordinary business note {index}."}
            for index in range(32)
        ]
        sections[-1]["content"] = "Contact alice@example.test for follow-up."
        with patch(
            "app.routers.upload.ner_detection.analyze_texts",
            side_effect=lambda texts, _confidence: {text: [] for text in texts},
        ):
            self.assertEqual(
                _document_sensitive_type({"sections": sections}),
                "EMAIL",
            )

    def test_sensitivity_classification_inspects_deduplicated_table_values(self):
        sections = [
            {
                "kind": "table_row",
                "heading_path": ["Customer Profiles"],
                "cells": [
                    {"content": "CUST-001"},
                    {"content": "alice@example.test"},
                ],
            },
            {
                "kind": "table_row",
                "heading_path": ["Customer Profiles"],
                "cells": [
                    {"content": "CUST-001"},
                    {"content": "alice@example.test"},
                ],
            },
            {
                "kind": "table_row",
                "heading_path": ["Customer Profiles"],
                "cells": [
                    {"content": "CUST-002"},
                    {"content": "bob@example.test"},
                ],
            },
        ]
        with patch(
            "app.routers.upload.ner_detection.analyze_texts",
            return_value={},
        ) as analyze:
            self.assertEqual(
                _document_sensitive_type({"sections": sections}),
                "EMAIL",
            )
        analyzed_texts = analyze.call_args.args[0]
        self.assertLessEqual(len(analyzed_texts), 3)

    def test_retrieval_masks_sensitive_text_and_excludes_document_values(self):
        doc = stored_document([self.section], action="mask", sensitive=True)
        tokenized = {
            self.section["content"]: "Contact [EMAIL_1] about the project delivery.",
            "Contact details": "Contact details",
        }
        with (
            patch("app.document_retrieval.mask_free_texts", side_effect=lambda texts, *_args, **_kwargs: {
                text: tokenized.get(text, text) for text in texts
            }),
            patch.object(mapping_store, "get_known_values", return_value={}),
            patch.object(mapping_store, "load_counters", return_value={}),
        ):
            context, sources, allowed = document_retrieval.retrieve_document_context(
                "chat-1", [doc], "project delivery"
            )
        self.assertIn("[EMAIL_1]", context)
        self.assertNotIn("alice@example.test", context)
        self.assertEqual(sources[0]["reference"], "D1:p-000001")
        self.assertEqual(sources[0]["page_number"], None)
        self.assertEqual(allowed, [])

        excluded = stored_document([self.section], action="exclude", sensitive=True)
        context, sources, allowed = document_retrieval.retrieve_document_context(
            "chat-1", [excluded], "project delivery"
        )
        self.assertEqual((context, sources, allowed), ("", [], []))

    def test_confirmed_share_is_explicitly_whitelisted_and_sources_are_safe(self):
        doc = stored_document([self.section], action="share", sensitive=True, confirmed=True)
        with (
            patch("app.document_retrieval.mask_free_texts", side_effect=lambda texts, *_args, **_kwargs: {
                text: text for text in texts
            }),
            patch.object(mapping_store, "get_known_values", return_value={}),
            patch.object(mapping_store, "load_counters", return_value={}),
        ):
            context, sources, allowed = document_retrieval.retrieve_document_context(
                "chat-1", [doc], "project delivery"
            )
        self.assertIn("alice@example.test", context)
        self.assertEqual(
            allowed,
            [json.dumps("Contact alice@example.test about the project delivery.", ensure_ascii=False)],
        )
        self.assertEqual(sources[0]["display_filename"], "Document 1")
        self.assertNotIn("alice@example.test", json.dumps(sources))
        validate_outbound_text(
            f"Question about this source.\n\n{context}",
            confirmed_share_segments=allowed,
        )
        with self.assertRaises(HTTPException):
            validate_outbound_text(
                f"Contact alice@example.test in the question.\n\n{context}",
                confirmed_share_segments=allowed,
            )

    def test_confirmed_share_with_escaped_quote_passes_outbound_validation(self):
        quoted_section = {
            **self.section,
            "content": 'Contact "alice@example.test" about the project delivery.',
        }
        doc = stored_document([quoted_section], action="share", sensitive=True, confirmed=True)
        with (
            patch("app.document_retrieval.mask_free_texts", side_effect=lambda texts, *_args, **_kwargs: {
                text: text for text in texts
            }),
            patch.object(mapping_store, "get_known_values", return_value={}),
            patch.object(mapping_store, "load_counters", return_value={}),
        ):
            context, _sources, allowed = document_retrieval.retrieve_document_context(
                "chat-1", [doc], "project delivery"
            )
        self.assertIn(r'\"alice@example.test\"', context)
        validate_outbound_text(context, confirmed_share_segments=allowed)

    def test_unconfirmed_sensitive_share_fails_closed(self):
        doc = stored_document([self.section], action="share", sensitive=True, confirmed=False)
        with self.assertRaises(HTTPException):
            document_retrieval.retrieve_document_context("chat-1", [doc], "project delivery")

    def test_excluded_docx_is_not_exposed_as_a_tool_sheet(self):
        doc = stored_document([
            {
                "source_id": "table-0001-row-0001",
                "kind": "table_row",
                "order": 1,
                "table_id": "table-0001",
                "heading_path": ["Transactions"],
                "is_header": True,
                "cells": [
                    {"source_id": "table-0001-row-0001-cell-001", "content": "Amount"},
                ],
            },
            {
                "source_id": "table-0001-row-0002",
                "kind": "table_row",
                "order": 2,
                "table_id": "table-0001",
                "heading_path": ["Transactions"],
                "is_header": False,
                "cells": [
                    {"source_id": "table-0001-row-0002-cell-001", "content": "48660"},
                ],
            },
        ], action="exclude")
        context = tools.ToolContext(
            user_id="doc-user",
            chat_id="chat-1",
            files={"doc-file-1": doc},
        )
        with patch.object(mapping_store, "get_chat", return_value={"chat_id": "chat-1"}):
            workbook = tools._load_tool_workbook(context, "doc-file-1")
        self.assertEqual(workbook.sheets, [])

    def test_uneven_docx_table_fails_closed_instead_of_misaligned_calculation(self):
        doc = stored_document([
            {
                "source_id": "table-0001-row-0001",
                "kind": "table_row",
                "order": 1,
                "table_id": "table-0001",
                "heading_path": ["Transactions"],
                "is_header": True,
                "cells": [
                    {"source_id": "table-0001-row-0001-cell-001", "content": "Amount"},
                    {"source_id": "table-0001-row-0001-cell-002", "content": "Status"},
                ],
            },
            {
                "source_id": "table-0001-row-0002",
                "kind": "table_row",
                "order": 2,
                "table_id": "table-0001",
                "heading_path": ["Transactions"],
                "is_header": False,
                "cells": [
                    {"source_id": "table-0001-row-0002-cell-001", "content": "48660"},
                ],
            },
        ], action="share")
        context = tools.ToolContext(
            user_id="doc-user",
            chat_id="chat-1",
            files={"doc-file-1": doc},
        )
        with patch.object(mapping_store, "get_chat", return_value={"chat_id": "chat-1"}):
            with self.assertRaises(tools.ToolExecutionError) as error:
                tools._load_tool_workbook(context, "doc-file-1")
        self.assertEqual(error.exception.code, "uneven_docx_table")

    def test_result_count_and_context_size_are_bounded(self):
        sections = [
            {
                **self.section,
                "source_id": f"p-{index:06d}",
                "order": index,
                "content": f"Unique item {index}: project delivery detail.",
            }
            for index in range(document_retrieval.MAX_DOCUMENT_SOURCES + 4)
        ]
        doc = stored_document(sections, action="share")
        with (
            patch("app.document_retrieval.mask_free_texts", side_effect=lambda texts, *_args, **_kwargs: {
                text: text for text in texts
            }),
            patch.object(mapping_store, "get_known_values", return_value={}),
            patch.object(mapping_store, "load_counters", return_value={}),
        ):
            context, sources, _allowed = document_retrieval.retrieve_document_context(
                "chat-1", [doc], "project delivery"
            )
        self.assertLessEqual(len(sources), document_retrieval.MAX_DOCUMENT_SOURCES)
        self.assertLessEqual(len(context), document_retrieval.MAX_DOCUMENT_CONTEXT_CHARS + 300)

    def test_broad_table_query_returns_all_matching_rows_in_document_order(self):
        sections = []
        for index, city in enumerate(["Pune", "Mumbai", "Pune", "Delhi", "Pune", "Mumbai", "Chennai", "Pune"], start=1):
            sections.append({
                "source_id": f"table-0001-row-{index:04d}",
                "kind": "table_row",
                "order": index,
                "table_id": "table-0001",
                "heading_path": ["Customer Profiles"],
                "cells": [
                    {"source_id": f"table-0001-row-{index:04d}-cell-001", "content": f"CUST-{index:03d}"},
                    {"source_id": f"table-0001-row-{index:04d}-cell-002", "content": city},
                ],
            })
        doc = stored_document(sections, action="share")
        with (
            patch("app.document_retrieval.mask_free_texts", side_effect=lambda texts, *_args, **_kwargs: {
                text: text for text in texts
            }),
            patch.object(mapping_store, "get_known_values", return_value={}),
            patch.object(mapping_store, "load_counters", return_value={}),
        ):
            context, sources, _allowed = document_retrieval.retrieve_document_context(
                "chat-1", [doc], "List all customer cities"
            )

        self.assertGreaterEqual(context.count("CUST-"), 8)
        self.assertEqual(len(sources), 8)
        for expected in ("Pune", "Mumbai", "Delhi", "Chennai"):
            self.assertIn(expected, context)

    def test_structured_docx_tools_compute_exact_transaction_totals(self):
        sections = [
            {
                "source_id": "table-0001-row-0001",
                "kind": "table_row",
                "order": 1,
                "table_id": "table-0001",
                "heading_path": ["Payment Transactions"],
                "is_header": True,
                "cells": [
                    {"source_id": "table-0001-row-0001-cell-001", "content": "Transaction ID"},
                    {"source_id": "table-0001-row-0001-cell-002", "content": "Amount"},
                    {"source_id": "table-0001-row-0001-cell-003", "content": "Status"},
                ],
            },
        ]
        amounts = ["12000", "9500", "11000", "8200", "7960"]
        statuses = ["Settled", "Settled", "Pending", "Refunded", "Settled"]
        for index, (amount, status) in enumerate(zip(amounts, statuses), start=2):
            sections.append({
                "source_id": f"table-0001-row-{index:04d}",
                "kind": "table_row",
                "order": index,
                "table_id": "table-0001",
                "heading_path": ["Payment Transactions"],
                "is_header": False,
                "cells": [
                    {"source_id": f"table-0001-row-{index:04d}-cell-001", "content": f"TXN-{index:03d}"},
                    {"source_id": f"table-0001-row-{index:04d}-cell-002", "content": amount},
                    {"source_id": f"table-0001-row-{index:04d}-cell-003", "content": status},
                ],
            })
        doc = stored_document(sections, action="share")
        context = tools.ToolContext(
            user_id="doc-user",
            chat_id="chat-1",
            files={"doc-file-1": doc},
        )

        with patch.object(mapping_store, "get_chat", return_value={"chat_id": "chat-1"}):
            workbook = tools._load_tool_workbook(context, "doc-file-1")
            schema = tools._available_schema(workbook)
            sheet = workbook.sheets[0]
            amount_column = next(column for column in sheet.columns if column.name == "Amount")
            status_column = next(column for column in sheet.columns if column.name == "Status")
            total = tools._aggregate_handler(context, tools.AggregateArgs(
                file_id="doc-file-1",
                worksheet_id=sheet.worksheet_id,
                aggregation="sum",
                column_id=amount_column.column_id,
            ))
            average = tools._aggregate_handler(context, tools.AggregateArgs(
                file_id="doc-file-1",
                worksheet_id=sheet.worksheet_id,
                aggregation="average",
                column_id=amount_column.column_id,
            ))
            status_groups = tools._group_aggregate(
                context,
                tools.GroupAggregateArgs(
                    file_id="doc-file-1",
                    worksheet_id=sheet.worksheet_id,
                    group_column_id=status_column.column_id,
                    aggregation="count",
                    limit=10,
                ),
            )

        self.assertEqual(schema["worksheets"][0]["row_count"], 5)
        self.assertEqual(total["value"], "48660")
        self.assertEqual(average["value"], "9732")
        groups = {item["group"]: item["value"] for item in status_groups["groups"]}
        self.assertEqual(groups, {"Pending": 1, "Refunded": 1, "Settled": 3})

    def test_docx_table_joins_to_csv_without_using_narrative_text(self):
        docx_table = DOCUMENT.replace("<w:t>Region</w:t>", "<w:t>Customer ID</w:t>").replace(
            "<w:t>Revenue</w:t>", "<w:t>Amount</w:t>"
        )
        doc = stored_document(docx_ingestion.parse_docx(make_docx(docx_table), "doc-file-1")["sections"], action="share")
        doc["file_id"] = "doc-file-1"
        doc_document = json.loads(doc["workbook_json"])
        doc_document["file_id"] = "doc-file-1"
        doc["workbook_json"] = json.dumps(doc_document)

        parsed = workbook_module.parse_workbook("revenue.csv", b"Customer ID,Sales\nWest,120.50\nEast,40")
        frame = parsed.worksheets[0].dataframe.copy()
        policy = {
            "Customer ID": {"action": "share", "sensitive": False, "share_confirmed": False},
            "Sales": {"action": "share", "sensitive": False, "share_confirmed": False},
        }
        workbook_document = workbook_module.build_workbook_document(
            parsed,
            "csv-file-1",
            policy,
            {"sheet-1": {"Customer ID": None, "Sales": None}},
            {"sheet-1": frame},
        )
        csv_file = {
            "file_id": "csv-file-1",
            "filename": "revenue.csv",
            "updated_at": 1.0,
            "workbook_json": workbook_module.serialize_workbook(workbook_document),
            "privacy_policy_json": json.dumps({"version": 1, "columns": policy}),
            "columns_json": "[]",
            "masked_csv": "",
            "row_count": 2,
        }
        doc.update({"filename": "revenue.docx", "updated_at": 1.0, "row_count": 1})
        context = tools.ToolContext(
            user_id="doc-user", chat_id="chat-1", files={"doc-file-1": doc, "csv-file-1": csv_file}
        )
        with patch.object(mapping_store, "get_chat", return_value={"chat_id": "chat-1"}), patch.object(
            mapping_store, "get_chat_file_revisions", return_value={"doc-file-1": 1.0, "csv-file-1": 1.0}
        ):
            doc_table = tools._load_tool_workbook(context, "doc-file-1").sheets[0]
            csv_sheet = tools._load_tool_workbook(context, "csv-file-1").sheets[0]
            doc_region = next(column for column in doc_table.columns if column.name == "Customer ID")
            csv_region = next(column for column in csv_sheet.columns if column.name == "Customer ID")
            doc_revenue = next(column for column in doc_table.columns if column.name == "Amount")
            csv_amount = next(column for column in csv_sheet.columns if column.name == "Sales")
            joined = tools.execute_tool(context, "join_tables", {
                "left_file_id": "doc-file-1", "left_worksheet_id": doc_table.worksheet_id,
                "left_key_column_id": doc_region.column_id,
                "right_file_id": "csv-file-1", "right_worksheet_id": csv_sheet.worksheet_id,
                "right_key_column_id": csv_region.column_id,
                "left_columns": [doc_revenue.column_id],
                "right_columns": [csv_amount.column_id],
            })

        self.assertEqual(joined["matched_row_count"], 1)
        self.assertEqual({source["source_format"] for source in joined["source_identities"]}, {"docx", "csv"})
        self.assertNotIn("team summary", json.dumps(joined))

    def test_document_revision_changes_when_content_changes(self):
        first = docx_ingestion.parse_docx(make_docx(), "doc-1")
        second = docx_ingestion.parse_docx(
            make_docx(DOCUMENT.replace("team summary", "updated team summary")),
            "doc-1",
        )
        self.assertNotEqual(first["document_revision"], second["document_revision"])


class EncryptedDocumentPersistenceTests(unittest.TestCase):
    def setUp(self):
        os.environ["PRIVY_ENVIRONMENT"] = "development"
        os.environ["PRIVY_ENCRYPTION_KEY"] = generate_key()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        TokenEntry.__table__.create(self.engine)
        Chat.__table__.create(self.engine)
        ChatFile.__table__.create(self.engine)
        self.original_factory = mapping_store.SessionLocal
        mapping_store.SessionLocal = self.factory
        with self.factory() as db:
            db.add(Chat(
                chat_id="doc-chat",
                user_id="doc-user",
                title="title",
                created_at=1,
                updated_at=1,
            ))
            db.commit()

    def tearDown(self):
        mapping_store.SessionLocal = self.original_factory
        self.engine.dispose()
        os.environ.pop("PRIVY_ENCRYPTION_KEY", None)

    def test_document_representation_is_encrypted_and_chat_scoped(self):
        representation = json.dumps({
            "schema_version": 1,
            "document_kind": "docx",
            "file_id": "doc-file",
            "sections": [{"source_id": "p-1", "content": "private document text"}],
        })
        mapping_store.set_chat_file(
            chat_id="doc-chat",
            file_id="doc-file",
            filename="private.docx",
            masked_csv="",
            columns_json="[]",
            row_count=1,
            truncated=False,
            privacy_policy_json='{"columns":{"document_content":{"action":"mask"}}}',
            workbook_json=representation,
        )
        with self.factory() as db:
            stored = db.scalar(select(ChatFile).where(ChatFile.file_id == "doc-file"))
            self.assertTrue(is_encrypted(stored.workbook_json))
            self.assertNotIn("private document text", stored.workbook_json)
            self.assertTrue(is_encrypted(stored.filename))

        restored = mapping_store.get_chat_file("doc-chat", "doc-file")
        self.assertEqual(restored["workbook_json"], representation)
        self.assertIsNone(mapping_store.get_chat_file("other-chat", "doc-file"))


if __name__ == "__main__":
    unittest.main()
