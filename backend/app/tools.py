"""Bounded, policy-aware local tools for workbook discovery and calculation."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Callable

import pandas as pd
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import mapping_store as store
from .privacy_policy import build_outbound_csv, load_policy
from .security_scan import scan_for_unmasked_pii
from .workbook import (
    column_identity,
    deserialize_workbook,
    workbook_policy_projection,
)

MAX_TOOL_CALLS_PER_REQUEST = int(os.getenv("PRIVY_MAX_TOOL_CALLS_PER_REQUEST", "6"))
MAX_TOOL_ROUNDS = int(os.getenv("PRIVY_MAX_TOOL_ROUNDS", "3"))
MAX_FILTER_CONDITIONS = int(os.getenv("PRIVY_MAX_FILTER_CONDITIONS", "10"))
MAX_FILTER_COLUMNS = int(os.getenv("PRIVY_MAX_FILTER_COLUMNS", "12"))
MAX_TOOL_ROWS = int(os.getenv("PRIVY_MAX_TOOL_ROWS", "25"))
MAX_TOOL_GROUPS = int(os.getenv("PRIVY_MAX_TOOL_GROUPS", "20"))
MAX_TOOL_RESULT_BYTES = int(os.getenv("PRIVY_MAX_TOOL_RESULT_BYTES", "12000"))
MAX_TOOL_ARGUMENT_BYTES = int(os.getenv("PRIVY_MAX_TOOL_ARGUMENT_BYTES", "8000"))
MAX_TOOL_EXECUTION_SECONDS = float(os.getenv("PRIVY_MAX_TOOL_EXECUTION_SECONDS", "10"))
MAX_JOIN_SOURCE_ROWS = max(1, min(int(os.getenv("PRIVY_MAX_JOIN_SOURCE_ROWS", "20000")), 20000))
MAX_JOIN_OUTPUT_ROWS = max(1, min(int(os.getenv("PRIVY_MAX_JOIN_OUTPUT_ROWS", "10000")), 10000))
MAX_JOIN_MEMORY_BYTES = max(1024, min(int(os.getenv("PRIVY_MAX_JOIN_MEMORY_BYTES", str(32 * 1024 * 1024))), 32 * 1024 * 1024))

_AGGREGATIONS = {"count", "sum", "average", "minimum", "maximum", "distinct_count"}
_FILTER_OPERATORS = {
    "eq", "ne", "gt", "gte", "lt", "lte", "contains", "starts_with", "is_null", "not_null",
}
_REDACTED = "[REDACTED]"
_MASKED = "[MASKED]"
_RESULT_SCHEMAS = {
    "get_workbook_schema": {
        "type": "object",
        "properties": {
            "workbooks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "workbook_id": {"type": "string"},
                        "worksheets": {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["workbook_id", "worksheets"],
                },
            },
        },
        "required": ["workbooks"],
    },
    "get_column_profile": {
        "type": "object",
        "properties": {
            "workbook_id": {"type": "string"},
            "worksheet_id": {"type": "string"},
            "column_id": {"type": "string"},
            "total_rows": {"type": "integer"},
            "non_null_count": {"type": "integer"},
            "null_count": {"type": "integer"},
            "distinct_count": {"type": "integer"},
            "exact": {"type": "boolean"},
            "minimum": {"type": "string"},
            "maximum": {"type": "string"},
            "top_categories": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["workbook_id", "worksheet_id", "column_id", "total_rows", "non_null_count", "null_count", "distinct_count", "exact"],
    },
    "filter_rows": {
        "type": "object",
        "properties": {
            "workbook_id": {"type": "string"},
            "worksheet_id": {"type": "string"},
            "exact": {"type": "boolean"},
            "matched_row_count": {"type": "integer"},
            "returned_row_count": {"type": "integer"},
            "truncated": {"type": "boolean"},
            "rows": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["workbook_id", "worksheet_id", "exact", "matched_row_count", "returned_row_count", "truncated", "rows"],
    },
    "aggregate_data": {
        "type": "object",
        "properties": {
            "workbook_id": {"type": "string"},
            "worksheet_id": {"type": "string"},
            "aggregation": {"type": "string"},
            "column_id": {"type": ["string", "null"]},
            "row_count": {"type": "integer"},
            "exact": {"type": "boolean"},
            "value": {"type": ["string", "integer", "number", "null"]},
            "non_null_count": {"type": "integer"},
        },
        "required": ["workbook_id", "worksheet_id", "aggregation", "row_count", "exact", "value", "non_null_count"],
    },
    "group_and_aggregate": {
        "type": "object",
        "properties": {
            "workbook_id": {"type": "string"},
            "worksheet_id": {"type": "string"},
            "aggregation": {"type": "string"},
            "row_count": {"type": "integer"},
            "group_count": {"type": "integer"},
            "exact": {"type": "boolean"},
            "groups": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["workbook_id", "worksheet_id", "aggregation", "row_count", "group_count", "exact", "groups"],
    },
    "compare_groups": {
        "type": "object",
        "properties": {
            "workbook_id": {"type": "string"},
            "worksheet_id": {"type": "string"},
            "aggregation": {"type": "string"},
            "row_count": {"type": "integer"},
            "exact": {"type": "boolean"},
            "comparisons": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["workbook_id", "worksheet_id", "aggregation", "row_count", "exact", "comparisons"],
    },
}


class ToolExecutionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.safe_message = message


class _StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SchemaArgs(_StrictArgs):
    file_id: str | None = Field(default=None, min_length=1, max_length=64)


class ColumnArgs(_StrictArgs):
    file_id: str = Field(min_length=1, max_length=64)
    worksheet_id: str = Field(min_length=1, max_length=64)
    column_id: str = Field(min_length=1, max_length=64)


class ProfileArgs(ColumnArgs):
    category_limit: int = Field(default=8, ge=1, le=10)


class FilterCondition(_StrictArgs):
    column_id: str = Field(min_length=1, max_length=64)
    operator: str
    value: str | int | float | bool | None = None


class FilterArgs(_StrictArgs):
    file_id: str = Field(min_length=1, max_length=64)
    worksheet_id: str = Field(min_length=1, max_length=64)
    conditions: list[FilterCondition] = Field(min_length=1, max_length=MAX_FILTER_CONDITIONS)
    match: str = "all"
    columns: list[str] = Field(min_length=1, max_length=MAX_FILTER_COLUMNS)
    limit: int = Field(default=MAX_TOOL_ROWS, ge=1, le=MAX_TOOL_ROWS)


class AggregateArgs(_StrictArgs):
    file_id: str = Field(min_length=1, max_length=64)
    worksheet_id: str = Field(min_length=1, max_length=64)
    aggregation: str
    column_id: str | None = Field(default=None, max_length=64)


class GroupAggregateArgs(_StrictArgs):
    file_id: str = Field(min_length=1, max_length=64)
    worksheet_id: str = Field(min_length=1, max_length=64)
    group_column_id: str = Field(min_length=1, max_length=64)
    aggregation: str
    metric_column_id: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=MAX_TOOL_GROUPS, ge=1, le=MAX_TOOL_GROUPS)


class CompareGroupsArgs(_StrictArgs):
    file_id: str = Field(min_length=1, max_length=64)
    worksheet_id: str = Field(min_length=1, max_length=64)
    group_column_id: str = Field(min_length=1, max_length=64)
    group_values: list[str] = Field(min_length=2, max_length=5)
    aggregation: str
    metric_column_id: str | None = Field(default=None, max_length=64)


class JoinDiscoveryArgs(_StrictArgs):
    file_ids: list[str] | None = Field(default=None, min_length=2, max_length=8)


class JoinArgs(_StrictArgs):
    left_file_id: str = Field(min_length=1, max_length=64)
    left_worksheet_id: str = Field(min_length=1, max_length=64)
    left_key_column_id: str = Field(min_length=1, max_length=64)
    right_file_id: str = Field(min_length=1, max_length=64)
    right_worksheet_id: str = Field(min_length=1, max_length=64)
    right_key_column_id: str = Field(min_length=1, max_length=64)
    left_columns: list[str] = Field(min_length=1, max_length=MAX_FILTER_COLUMNS)
    right_columns: list[str] = Field(min_length=1, max_length=MAX_FILTER_COLUMNS)


class JoinedAggregateArgs(_StrictArgs):
    result_ref: str = Field(min_length=36, max_length=36)
    aggregation: str
    column_ref: str | None = Field(default=None, max_length=140)


@dataclass
class ToolColumn:
    column_id: str
    policy_key: str
    name: str
    data_type: str | None
    action: str
    sensitive: bool
    share_confirmed: bool
    position: int

    @property
    def protected(self) -> bool:
        return self.action == "mask" or self.sensitive


@dataclass
class ToolWorksheet:
    worksheet_id: str
    name: str
    position: int
    row_count: int
    column_count: int
    visibility: str
    is_irregular: bool
    columns: list[ToolColumn]
    frame: pd.DataFrame | None = None


@dataclass
class ToolWorkbook:
    file_id: str
    sheets: list[ToolWorksheet]
    source_workbook: dict[str, Any] | None = None
    privacy_policy: dict[str, dict] = field(default_factory=dict)


@dataclass
class ToolContext:
    user_id: str
    chat_id: str
    files: dict[str, dict[str, Any]]
    workbooks: dict[str, ToolWorkbook] = field(default_factory=dict)
    execution_count: int = 0
    join_results: dict[str, "JoinedToolResult"] = field(default_factory=dict)
    join_result_bytes: int = 0
    pending_join_confirmations: list[dict[str, Any]] = field(default_factory=list)
    approved_join_confirmation_id: str | None = None


@dataclass
class JoinedToolResult:
    result_id: str
    owner_id: str
    chat_id: str
    frame: pd.DataFrame
    columns: dict[str, ToolColumn]
    sources: list[dict[str, Any]]
    created_at: float
    memory_bytes: int
    transformations: list[str]


def cleanup_tool_context(context: ToolContext) -> None:
    """Drop request-scoped derived data on success, failure, or cancellation."""
    context.join_results.clear()
    context.join_result_bytes = 0
    context.workbooks.clear()
    context.pending_join_confirmations.clear()
    context.approved_join_confirmation_id = None


def _discard_join_result(context: ToolContext, result_ref: str | None) -> None:
    if not result_ref:
        return
    dataset = context.join_results.pop(result_ref, None)
    if dataset is not None:
        context.join_result_bytes = max(0, context.join_result_bytes - dataset.memory_bytes)


def _source_identity(context: ToolContext, file_id: str, worksheet_id: str) -> dict[str, Any]:
    """Build a request-scoped identity without including data values."""
    workbook = _load_tool_workbook(context, file_id)
    item = context.files[file_id]
    revision = item.get("updated_at")
    source = workbook.source_workbook or {}
    extension = os.path.splitext(str(item.get("filename") or ""))[1].lower().lstrip(".")
    source_format = "docx" if source.get("document_kind") == "docx" else (
        str(source.get("source_format") or item.get("file_type") or extension or "spreadsheet").lower()
    )
    policy_bytes = str(item.get("privacy_policy_json") or "").encode("utf-8")
    sheet = next((entry for entry in workbook.sheets if entry.worksheet_id == worksheet_id), None)
    return {
        "file_id": file_id,
        "ownership": "current_authenticated_user",
        "ownership_verified": True,
        "file_revision": str(revision) if revision is not None else None,
        "source_revision": source.get("document_revision") or source.get("revision"),
        "source_format": source_format,
        "table_id": worksheet_id,
        "column_ids": [column.column_id for column in sheet.columns] if sheet else [],
        "privacy_policy_revision": hashlib.sha256(policy_bytes).hexdigest()[:16],
    }


def _attach_result_contract(context: ToolContext, operation: str, arguments: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Add common verification/provenance fields while retaining legacy result keys."""
    file_id = arguments.get("file_id")
    worksheet_id = arguments.get("worksheet_id")
    identities = list(result.get("source_identities", [])) if isinstance(result.get("source_identities"), list) else []
    if isinstance(file_id, str) and isinstance(worksheet_id, str) and file_id in context.files:
        identities.append(_source_identity(context, file_id, worksheet_id))
    elif isinstance(file_id, str) and file_id in context.files:
        for sheet in _get_workbook(context, file_id).sheets:
            identities.append(_source_identity(context, file_id, sheet.worksheet_id))
    elif operation == "discover_joins":
        for source_file_id in arguments.get("file_ids") or sorted(context.files):
            if source_file_id in context.files:
                for sheet in _get_workbook(context, source_file_id).sheets:
                    identities.append(_source_identity(context, source_file_id, sheet.worksheet_id))
    for candidate in result.get("candidates", []):
        if not isinstance(candidate, dict):
            continue
        for side in ("left", "right"):
            source = candidate.get(side)
            if isinstance(source, dict) and source.get("file_id") in context.files:
                identity = _source_identity(context, source["file_id"], source.get("worksheet_id", ""))
                if identity not in identities:
                    identities.append(identity)
    for source in result.get("join_sources", []):
        if isinstance(source, dict) and source.get("file_id") in context.files:
            identity = _source_identity(context, source["file_id"], source.get("worksheet_id", ""))
            if identity not in identities:
                identities.append(identity)
    conditions = arguments.get("conditions") or []
    result.update({
        "operation": operation,
        "operation_version": 1,
        "verification_status": "exact" if result.get("exact") is True else "not_verified",
        "source_identities": identities,
        "privacy_policy_revisions": sorted({item["privacy_policy_revision"] for item in identities}),
        "provenance": {
            "source_tables": [item["table_id"] for item in identities],
            "transformations": [*result.pop("prior_transformations", []), operation],
            "filters": [
                {"column_id": condition.get("column_id"), "operator": condition.get("operator")}
                for condition in conditions if isinstance(condition, dict)
            ],
            "join_keys": ([
                {"file_id": arguments.get("left_file_id"), "worksheet_id": arguments.get("left_worksheet_id"), "column_id": arguments.get("left_key_column_id")},
                {"file_id": arguments.get("right_file_id"), "worksheet_id": arguments.get("right_worksheet_id"), "column_id": arguments.get("right_key_column_id")},
            ] if operation == "join_tables" else []),
        },
        "input_record_count": result.get("row_count", result.get("total_rows", (
            (result.get("left_row_count") or 0) + (result.get("right_row_count") or 0)
            if operation == "join_tables" else None
        ))),
        "output_record_count": result.get("matched_row_count", result.get("group_count", result.get("row_count", result.get("returned_row_count")))),
        "returned_record_count": result.get("returned_row_count"),
        "warnings": result.get("warnings", []),
        "validation_checks": [*result.get("validation_checks", []), "chat_ownership", "file_ownership", "privacy_policy", "bounded_result", "outbound_pii_scan"],
        "execution_metadata": {"engine": "pandas", "bounded": True},
        "partial_result": bool(result.get("truncated", False)),
        "limitations": (["Result rows were capped by the configured tool limit."] if result.get("truncated") else []),
    })
    result.setdefault("privacy_policy_revision", identities[0]["privacy_policy_revision"] if identities else None)
    return result


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    required_permissions: frozenset[str]
    argument_model: type[BaseModel]
    handler: Callable[[ToolContext, BaseModel], dict[str, Any]]
    result_schema: dict[str, Any]
    max_result_bytes: int

    def provider_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


_PRIMITIVE = {
    "type": ["string", "number", "integer", "boolean", "null"],
    "description": "A literal value; use a string for dates and text.",
}


def _column_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "file_id": {"type": "string"},
            "worksheet_id": {"type": "string"},
            "column_id": {"type": "string"},
        },
        "required": ["file_id", "worksheet_id", "column_id"],
        "additionalProperties": False,
    }


def _aggregation_schema() -> dict[str, Any]:
    return {
        "type": "string",
        "enum": sorted(_AGGREGATIONS),
    }


def _check_choice(choice: dict, data_type: str | None) -> None:
    from .privacy_policy import recommendation

    _recommended, sensitive, _reason = recommendation(data_type)
    if (
        not isinstance(choice, dict)
        or choice.get("action") not in {"share", "mask", "exclude"}
        or choice.get("sensitive") != sensitive
        or not isinstance(choice.get("share_confirmed"), bool)
    ):
        raise ToolExecutionError("invalid_privacy_policy", "The workbook privacy policy is invalid.")
    if choice["share_confirmed"] and (choice["action"] != "share" or not sensitive):
        raise ToolExecutionError("invalid_privacy_policy", "The workbook privacy policy is invalid.")
    if choice["action"] == "share" and sensitive and not choice["share_confirmed"]:
        raise ToolExecutionError("unconfirmed_sensitive_share", "A sensitive Share choice needs confirmation.")


def create_tool_context(
    chat_id: str,
    user_id: str,
    files: list[dict[str, Any]] | None = None,
    approved_join_confirmation_id: str | None = None,
) -> ToolContext:
    if not store.get_chat(chat_id, user_id):
        raise ToolExecutionError("chat_not_found", "The requested chat is not available.")
    if files is None:
        files = store.get_chat_files(chat_id)
    context = ToolContext(
        user_id=user_id,
        chat_id=chat_id,
        files={str(item["file_id"]): item for item in files},
        approved_join_confirmation_id=approved_join_confirmation_id,
    )
    _refresh_authorized_files(context, None)
    return context


def _column_from_stored(stored: dict, choice: dict) -> ToolColumn:
    _check_choice(choice, stored.get("data_type"))
    return ToolColumn(
        column_id=str(stored["column_id"]),
        policy_key=str(stored["policy_key"]),
        name=str(stored["display_name"]),
        data_type=stored.get("data_type"),
        action=str(choice["action"]),
        sensitive=bool(choice["sensitive"]),
        share_confirmed=bool(choice["share_confirmed"]),
        position=int(stored["position"]),
    )


def _load_tool_workbook(context: ToolContext, file_id: str) -> ToolWorkbook:
    cached = context.workbooks.get(file_id)
    if cached is not None:
        return cached
    chat_file = context.files.get(file_id)
    if chat_file is None:
        raise ToolExecutionError("file_not_found", "That workbook is not attached to this chat.")

    workbook_json = chat_file.get("workbook_json")
    if workbook_json:
        try:
            raw_document = json.loads(workbook_json)
        except (TypeError, ValueError):
            raw_document = None
        if isinstance(raw_document, dict) and raw_document.get("document_kind") == "docx":
            if raw_document.get("file_id") != file_id:
                raise ToolExecutionError("invalid_workbook", "The stored document identity is invalid.")
            policy, legacy = load_policy(
                chat_file.get("privacy_policy_json"),
                chat_file.get("columns_json"),
            )
            if legacy:
                raise ToolExecutionError("missing_privacy_policy", "The document privacy policy needs review.")
            choice = policy.get("document_content")
            if not isinstance(choice, dict):
                raise ToolExecutionError("invalid_privacy_policy", "The document privacy policy is invalid.")
            if choice["action"] == "exclude":
                result = ToolWorkbook(file_id=file_id, sheets=[], source_workbook=raw_document, privacy_policy=policy)
                context.workbooks[file_id] = result
                return result
            if choice["action"] == "share" and choice.get("sensitive") and not choice.get("share_confirmed"):
                raise ToolExecutionError("unconfirmed_sensitive_share", "A sensitive document Share choice needs confirmation.")

            worksheets: list[ToolWorksheet] = []
            table_sections: dict[str, list[dict[str, Any]]] = {}
            table_names: dict[str, str] = {}
            for section in raw_document.get("sections", []):
                if not isinstance(section, dict) or section.get("kind") != "table_row":
                    continue
                table_id = str(section.get("table_id") or "")
                if not table_id:
                    continue
                table_sections.setdefault(table_id, []).append(section)
                heading_path = section.get("heading_path", [])
                if heading_path and table_id not in table_names:
                    table_names[table_id] = str(heading_path[-1])

            for table_position, (table_id, rows) in enumerate(sorted(
                table_sections.items(),
                key=lambda item: min(int(row.get("order", 0)) for row in item[1]),
            )):
                ordered = sorted(rows, key=lambda row: int(row.get("order", 0)))
                if not ordered:
                    continue
                if len({len(row.get("cells", [])) for row in ordered}) > 1:
                    raise ToolExecutionError(
                        "uneven_docx_table",
                        "This DOCX table has uneven row widths and could not be analyzed safely.",
                    )
                header_rows = [row for row in ordered if row.get("is_header") is True]
                if header_rows:
                    first_cells = header_rows[-1].get("cells", [])
                else:
                    first_cells = []
                if first_cells:
                    headers = [
                        str(cell.get("content", "")).strip() or f"Column {column_position + 1}"
                        for column_position, cell in enumerate(first_cells)
                    ]
                    header_column_count = len(headers)
                    uneven_rows = [
                        row for row in ordered
                        if len(row.get("cells", [])) not in {header_column_count}
                    ]
                    if uneven_rows:
                        raise ToolExecutionError(
                            "uneven_docx_table",
                            "This DOCX table has uneven row widths and could not be analyzed safely.",
                        )
                else:
                    column_count = max(
                        (len(row.get("cells", [])) for row in ordered),
                        default=0,
                    )
                    headers = [f"Column {column_position + 1}" for column_position in range(column_count)]
                unique_headers: list[str] = []
                for header_position, header in enumerate(headers):
                    normalized = header.casefold()
                    count = sum(
                        1 for existing in unique_headers if existing.casefold() == normalized
                    )
                    unique_headers.append(
                        header if count == 0
                        else f"{header} ({count + 1})"
                    )
                headers = unique_headers
                columns: list[ToolColumn] = []
                for column_position, header in enumerate(headers):
                    policy_key = f"{table_id}:column-{column_position + 1}"
                    columns.append(ToolColumn(
                        column_id=f"{table_id}:column-{column_position + 1}",
                        policy_key=policy_key,
                        name=header,
                        data_type=None,
                        action=str(choice["action"]),
                        sensitive=bool(choice.get("sensitive")),
                        share_confirmed=bool(choice.get("share_confirmed")),
                        position=column_position,
                    ))
                worksheets.append(ToolWorksheet(
                    worksheet_id=table_id,
                    name=table_names.get(table_id, f"Table {table_position + 1}"),
                    position=table_position,
                    row_count=max(0, len(ordered) - 1),
                    column_count=len(columns),
                    visibility="visible",
                    is_irregular=False,
                    columns=columns,
                ))
            result = ToolWorkbook(
                file_id=file_id,
                sheets=worksheets,
                source_workbook=raw_document,
                privacy_policy=policy,
            )
            context.workbooks[file_id] = result
            return result
        workbook_data = deserialize_workbook(workbook_json)
        if workbook_data.get("file_id") != file_id:
            raise ToolExecutionError("invalid_workbook", "The stored workbook identity is invalid.")
        policy, legacy = load_policy(
            chat_file.get("privacy_policy_json"),
            chat_file.get("columns_json"),
        )
        if legacy:
            raise ToolExecutionError("missing_privacy_policy", "The workbook privacy policy needs review.")
        projected = workbook_policy_projection(
            workbook_data,
            policy,
            selected_policy_keys=set(),
            include_rows=False,
        )
        worksheets: list[ToolWorksheet] = []
        for stored_sheet, safe_sheet in zip(workbook_data["worksheets"], projected):
            columns = [
                _column_from_stored(column, policy[column["policy_key"]])
                for column in stored_sheet["columns"]
                if policy[column["policy_key"]]["action"] != "exclude"
            ]
            worksheets.append(ToolWorksheet(
                worksheet_id=safe_sheet["worksheet_id"],
                name=str(stored_sheet.get("original_name") or f"Worksheet {safe_sheet['position'] + 1}"),
                position=safe_sheet["position"],
                row_count=safe_sheet["row_count"],
                column_count=safe_sheet["column_count"],
                visibility=safe_sheet["visibility"],
                is_irregular=safe_sheet["is_irregular"],
                columns=columns,
            ))
        result = ToolWorkbook(
            file_id=file_id,
            sheets=worksheets,
            source_workbook=workbook_data,
            privacy_policy=policy,
        )
    else:
        result = _load_legacy_csv(chat_file, file_id)
    context.workbooks[file_id] = result
    return result


def _load_legacy_csv(chat_file: dict, file_id: str) -> ToolWorkbook:
    try:
        metadata = json.loads(chat_file.get("columns_json") or "[]")
    except (TypeError, ValueError) as exc:
        raise ToolExecutionError("invalid_workbook", "The stored workbook metadata is invalid.") from exc
    policy, legacy = load_policy(
        chat_file.get("privacy_policy_json"),
        chat_file.get("columns_json"),
    )
    if not isinstance(metadata, list):
        raise ToolExecutionError("invalid_workbook", "The stored workbook metadata is invalid.")
    columns: list[ToolColumn] = []
    for position, item in enumerate(metadata):
        if not isinstance(item, dict):
            raise ToolExecutionError("invalid_workbook", "The stored workbook metadata is invalid.")
        name = str(item.get("name") or "")
        choice = policy.get(name)
        if choice is None:
            raise ToolExecutionError("invalid_privacy_policy", "The workbook privacy policy is invalid.")
        if legacy:
            choice = {**choice, "share_confirmed": True}
        _check_choice(choice, item.get("type"))
        if choice["action"] != "exclude":
            columns.append(ToolColumn(
                column_id=str(item.get("column_id") or column_identity(0, position, name)),
                policy_key=name,
                name=name,
                data_type=item.get("type"),
                action=str(choice["action"]),
                sensitive=bool(choice["sensitive"]),
                share_confirmed=bool(choice["share_confirmed"] or legacy),
                position=position,
            ))
    if set(policy) != {str(item.get("name") or "") for item in metadata if isinstance(item, dict)}:
        raise ToolExecutionError("invalid_privacy_policy", "The workbook privacy policy is invalid.")
    return ToolWorkbook(
        file_id=file_id,
        sheets=[ToolWorksheet(
            worksheet_id="sheet-1",
            name="CSV",
            position=0,
            row_count=int(chat_file.get("row_count") or 0),
            column_count=len(metadata),
            visibility="visible",
            is_irregular=False,
            columns=columns,
        )],
    )


def _ensure_frame(context: ToolContext, file_id: str, sheet: ToolWorksheet) -> pd.DataFrame:
    if sheet.frame is not None:
        return sheet.frame
    chat_file = context.files[file_id]
    workbook_json = chat_file.get("workbook_json")
    if workbook_json:
        workbook = _load_tool_workbook(context, file_id)
        workbook_data = workbook.source_workbook
        if workbook_data is None:
            raise ToolExecutionError("invalid_workbook", "The stored workbook is unavailable.")
        if isinstance(workbook_data, dict) and workbook_data.get("document_kind") == "docx":
            rows: list[dict[str, Any]] = []
            table_rows = [
                section
                for section in workbook_data.get("sections", [])
                if isinstance(section, dict)
                and section.get("kind") == "table_row"
                and str(section.get("table_id") or "") == sheet.worksheet_id
            ]
            table_rows.sort(key=lambda row: int(row.get("order", 0)))
            if table_rows:
                data_rows = [
                    row for row in table_rows
                    if row.get("is_header") is not True
                ]
                for row in data_rows:
                    values: dict[str, Any] = {}
                    cells = row.get("cells", [])
                    for column in sheet.columns:
                        column_position = column.position
                        cell = cells[column_position] if column_position < len(cells) else None
                        value = str(cell.get("content", "")).strip() if isinstance(cell, dict) else ""
                        if column.action == "mask" and scan_for_unmasked_pii(value):
                            raise ToolExecutionError(
                                "privacy_validation_failed",
                                "The document result did not pass outbound privacy validation.",
                            )
                        values[column.policy_key] = value
                    rows.append(values)
            frame = pd.DataFrame(
                rows,
                columns=[column.policy_key for column in sheet.columns],
            )
            sheet.row_count = len(frame)
            sheet.frame = frame
            return frame
        policy = workbook.privacy_policy
        selected_keys = {column.policy_key for column in sheet.columns}
        projected = workbook_policy_projection(
            workbook_data,
            policy,
            selected_keys,
            worksheet_ids={sheet.worksheet_id},
        )
        projection_sheet = next(
            (item for item in projected if item["worksheet_id"] == sheet.worksheet_id),
            None,
        )
        if projection_sheet is None:
            raise ToolExecutionError("worksheet_not_found", "That worksheet is not available.")
        frames = {
            column.policy_key: [
                row["values"][index]
                for row in projection_sheet["rows"]
            ]
            for index, column in enumerate(sheet.columns)
        }
        frame = pd.DataFrame(frames, columns=[column.policy_key for column in sheet.columns])
        sheet.row_count = len(frame)
    else:
        policy, legacy = load_policy(
            chat_file.get("privacy_policy_json"),
            chat_file.get("columns_json"),
        )
        safe_csv = build_outbound_csv(
            str(chat_file.get("masked_csv") or ""),
            policy,
            legacy=legacy,
        )
        reader = csv.DictReader(io.StringIO(safe_csv, newline=""))
        data = list(reader)
        rows = {
            column.policy_key: [
                row.get(column.policy_key) or None
                for row in data
            ]
            for column in sheet.columns
        }
        frame = pd.DataFrame(rows, columns=[column.policy_key for column in sheet.columns])
        sheet.row_count = len(frame)
    if len(frame) != sheet.row_count:
        raise ToolExecutionError("incomplete_workbook", "The complete stored workbook could not be verified.")
    sheet.frame = frame
    return frame


def _get_workbook(context: ToolContext, file_id: str) -> ToolWorkbook:
    if file_id not in context.files:
        raise ToolExecutionError("file_not_found", "That workbook is not attached to this chat.")
    return _load_tool_workbook(context, file_id)


def _refresh_authorized_files(context: ToolContext, file_id: str | None) -> None:
    revisions = store.get_chat_file_revisions(context.chat_id)
    if file_id is not None:
        if file_id not in revisions:
            context.files.pop(file_id, None)
            context.workbooks.pop(file_id, None)
            raise ToolExecutionError("file_not_found", "That workbook is not attached to this chat.")
        file_ids = [file_id]
    else:
        file_ids = list(set(context.files) | set(revisions))
    for current_id in file_ids:
        revision = revisions.get(current_id)
        cached_file = context.files.get(current_id)
        if revision is None:
            context.files.pop(current_id, None)
            context.workbooks.pop(current_id, None)
        elif cached_file is None or float(cached_file.get("updated_at") or 0) != revision:
            current_file = store.get_chat_file(context.chat_id, current_id)
            if current_file is None:
                context.files.pop(current_id, None)
                context.workbooks.pop(current_id, None)
            else:
                context.files[current_id] = current_file
                context.workbooks.pop(current_id, None)


def _get_sheet(context: ToolContext, file_id: str, worksheet_id: str) -> ToolWorksheet:
    workbook = _get_workbook(context, file_id)
    sheet = next((item for item in workbook.sheets if item.worksheet_id == worksheet_id), None)
    if sheet is None:
        raise ToolExecutionError("worksheet_not_found", "That worksheet is not available.")
    return sheet


def _get_column(sheet: ToolWorksheet, column_id: str) -> ToolColumn:
    column = next((item for item in sheet.columns if item.column_id == column_id), None)
    if column is None:
        raise ToolExecutionError("column_not_found", "That column is excluded or is not available.")
    if column.action == "exclude":
        raise ToolExecutionError("column_excluded", "That column is excluded by the privacy policy.")
    return column


def _schema_name(value: str, position: int, label: str = "Column") -> str:
    return "[sensitive label hidden]" if scan_for_unmasked_pii(value) else value


def _available_schema(workbook: ToolWorkbook) -> dict[str, Any]:
    return {
        "workbook_id": workbook.file_id,
        "worksheets": [
            {
                "worksheet_id": sheet.worksheet_id,
                "name": _schema_name(sheet.name, sheet.position, "Worksheet"),
                "row_count": sheet.row_count,
                "column_count": sheet.column_count,
                "available_column_count": len(sheet.columns),
                "visibility": sheet.visibility,
                "warnings": ["irregular row widths normalized"] if sheet.is_irregular else [],
                "columns": [
                    {
                        "column_id": column.column_id,
                        "name": _schema_name(column.name, column.position),
                        "data_type": column.data_type,
                        "privacy_action": column.action,
                        "sensitive": column.sensitive,
                    }
                    for column in sheet.columns
                ],
            }
            for sheet in workbook.sheets
        ],
    }


def describe_workbooks(context: ToolContext) -> str:
    schemas = [_available_schema(_load_tool_workbook(context, file_id)) for file_id in context.files]
    if not schemas:
        return "No workbook is attached."
    return (
        "Attached workbook schemas (only columns permitted by each privacy policy are listed). "
        "Use the workbook_id, worksheet_id, and column_id values with the local tools. "
        "All exact calculations must use these tools; no data rows are included here:\n"
        + json.dumps(schemas, ensure_ascii=True, separators=(",", ":"))
    )


def _discover_joins(context: ToolContext, raw_args: BaseModel) -> dict[str, Any]:
    args = raw_args
    assert isinstance(args, JoinDiscoveryArgs)
    file_ids = args.file_ids or sorted(context.files)
    if len(file_ids) < 2 or len(file_ids) > 8 or len(set(file_ids)) != len(file_ids):
        raise ToolExecutionError("invalid_sources", "Choose between two and eight distinct attached files.")
    workbooks = [_get_workbook(context, file_id) for file_id in file_ids]
    candidates: list[dict[str, Any]] = []
    for left_index, left_book in enumerate(workbooks):
        for right_book in workbooks[left_index + 1:]:
            for left_sheet in left_book.sheets:
                if left_sheet.row_count > MAX_JOIN_SOURCE_ROWS:
                    continue
                left_frame = _ensure_frame(context, left_book.file_id, left_sheet)
                if len(left_frame) > 20000:
                    continue
                for right_sheet in right_book.sheets:
                    if right_sheet.row_count > MAX_JOIN_SOURCE_ROWS:
                        continue
                    right_frame = _ensure_frame(context, right_book.file_id, right_sheet)
                    if len(right_frame) > 20000:
                        continue
                    for left_col in left_sheet.columns:
                        if left_col.action != "share" or (left_col.sensitive and not left_col.share_confirmed):
                            continue
                        left_values = _non_null(_series(left_sheet, left_col, left_frame))
                        if not left_values:
                            continue
                        left_kind = _column_kind(left_values)
                        for right_col in right_sheet.columns:
                            if right_col.action != "share" or (right_col.sensitive and not right_col.share_confirmed):
                                continue
                            if re.sub(r"[^a-z0-9]+", "", left_col.name.casefold()) != re.sub(r"[^a-z0-9]+", "", right_col.name.casefold()):
                                continue
                            right_values = _non_null(_series(right_sheet, right_col, right_frame))
                            if not right_values or _column_kind(right_values) != left_kind:
                                continue
                            left_series = _series(left_sheet, left_col, left_frame)
                            right_series = _series(right_sheet, right_col, right_frame)
                            left_non_null = [str(v) for v in left_series if v is not None and not pd.isna(v)]
                            right_non_null = [str(v) for v in right_series if v is not None and not pd.isna(v)]
                            left_unique = len(left_non_null) == len(set(left_non_null))
                            right_unique = len(right_non_null) == len(set(right_non_null))
                            cardinality = "one_to_one" if left_unique and right_unique else (
                                "one_to_many" if left_unique else "many_to_one" if right_unique else "many_to_many"
                            )
                            candidates.append({
                                "left": {"file_id": left_book.file_id, "worksheet_id": left_sheet.worksheet_id, "column_id": left_col.column_id},
                                "right": {"file_id": right_book.file_id, "worksheet_id": right_sheet.worksheet_id, "column_id": right_col.column_id},
                                "compatible_type": left_kind,
                                "left_null_count": int(len(left_series) - len(left_non_null)),
                                "right_null_count": int(len(right_series) - len(right_non_null)),
                                "left_unique": left_unique,
                                "right_unique": right_unique,
                                "observed_cardinality": cardinality,
                                "overlap_count": len(set(left_non_null) & set(right_non_null)),
                                "requires_confirmation": False,
                                "execution_eligible": cardinality != "many_to_many" and bool(set(left_non_null) & set(right_non_null)),
                            })
    candidate_counts: dict[tuple[str, str, str, str], int] = {}
    visible_candidates = candidates[:40]
    for candidate in visible_candidates:
        left = candidate["left"]
        right = candidate["right"]
        pair = (left["file_id"], left["worksheet_id"], right["file_id"], right["worksheet_id"])
        candidate_counts[pair] = candidate_counts.get(pair, 0) + 1
    for candidate in candidates:
        left = candidate["left"]
        right = candidate["right"]
        pair = (left["file_id"], left["worksheet_id"], right["file_id"], right["worksheet_id"])
        ambiguous = candidate_counts[pair] > 1
        candidate["requires_confirmation"] = ambiguous
        candidate["left_column_label"] = _schema_name(
            _get_column(_get_sheet(context, left["file_id"], left["worksheet_id"]), left["column_id"]).name, 0
        )
        candidate["right_column_label"] = _schema_name(
            _get_column(_get_sheet(context, right["file_id"], right["worksheet_id"]), right["column_id"]).name, 0
        )
        if ambiguous and candidate["execution_eligible"]:
            identities = [
                _source_identity(context, left["file_id"], left["worksheet_id"]),
                _source_identity(context, right["file_id"], right["worksheet_id"]),
            ]
            binding = {"left": left, "right": right, "source_identities": identities}
            confirmation_id = store.create_join_confirmation(
                chat_id=context.chat_id, user_id=context.user_id, candidate=binding, ttl_seconds=300
            )
            context.pending_join_confirmations.append({
                "confirmation_id": confirmation_id,
                "left": left,
                "right": right,
                "left_column_label": candidate["left_column_label"],
                "right_column_label": candidate["right_column_label"],
                "expires_in_seconds": 300,
            })
    return {"candidates": visible_candidates, "exact": True, "candidate_limit_reached": len(candidates) > 40}


def _join_tables(context: ToolContext, raw_args: BaseModel) -> dict[str, Any]:
    args = raw_args
    assert isinstance(args, JoinArgs)
    if args.left_file_id == args.right_file_id:
        raise ToolExecutionError("invalid_sources", "A join requires two distinct attached files.")
    left_sheet = _get_sheet(context, args.left_file_id, args.left_worksheet_id)
    right_sheet = _get_sheet(context, args.right_file_id, args.right_worksheet_id)
    left_key = _get_column(left_sheet, args.left_key_column_id)
    right_key = _get_column(right_sheet, args.right_key_column_id)
    if left_key.action != "share" or right_key.action != "share":
        raise ToolExecutionError("ineligible_join_key", "Join keys must be explicitly Share-permitted columns.")
    left_frame = _ensure_frame(context, args.left_file_id, left_sheet)
    right_frame = _ensure_frame(context, args.right_file_id, right_sheet)
    if len(left_frame) > MAX_JOIN_SOURCE_ROWS or len(right_frame) > MAX_JOIN_SOURCE_ROWS:
        raise ToolExecutionError("join_source_too_large", "Join sources exceed the configured row safety limit.")

    def normalized(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.casefold())

    left_values = _series(left_sheet, left_key, left_frame)
    right_values = _series(right_sheet, right_key, right_frame)
    left_non_null = _non_null(left_values)
    right_non_null = _non_null(right_values)
    if not left_non_null or not right_non_null or _column_kind(left_non_null) != _column_kind(right_non_null):
        raise ToolExecutionError("incompatible_join_keys", "Join keys have no compatible, non-null values.")

    # A unique compatible name pair is the only relationship that can execute
    # without an interactive confirmation channel. Ambiguity fails closed.
    key_candidates = []
    for left_col in left_sheet.columns:
        if left_col.action != "share":
            continue
        lv = _non_null(_series(left_sheet, left_col, left_frame))
        for right_col in right_sheet.columns:
            if right_col.action != "share" or normalized(left_col.name) != normalized(right_col.name):
                continue
            rv = _non_null(_series(right_sheet, right_col, right_frame))
            if lv and rv and _column_kind(lv) == _column_kind(rv):
                key_candidates.append((left_col.column_id, right_col.column_id))
    selected_key = (left_key.column_id, right_key.column_id)
    if selected_key not in key_candidates:
        raise ToolExecutionError("ambiguous_join", "The selected relationship is ambiguous and needs confirmation.")
    if len(key_candidates) > 1:
        confirmation_id = context.approved_join_confirmation_id
        if not confirmation_id:
            raise ToolExecutionError("ambiguous_join", "This relationship needs explicit user confirmation before it can be joined.")
        confirmed = store.consume_join_confirmation(
            chat_id=context.chat_id, user_id=context.user_id, confirmation_id=confirmation_id
        )
        context.approved_join_confirmation_id = None
        current_binding = {
            "left": {"file_id": args.left_file_id, "worksheet_id": args.left_worksheet_id, "column_id": left_key.column_id},
            "right": {"file_id": args.right_file_id, "worksheet_id": args.right_worksheet_id, "column_id": right_key.column_id},
            "source_identities": [
                _source_identity(context, args.left_file_id, args.left_worksheet_id),
                _source_identity(context, args.right_file_id, args.right_worksheet_id),
            ],
        }
        if confirmed != current_binding:
            raise ToolExecutionError("invalid_confirmation", "The join confirmation is expired, reused, stale, or does not match this relationship.")

    if normalized(left_key.name) != normalized(right_key.name):
        raise ToolExecutionError("ambiguous_join", "The relationship is ambiguous and needs user confirmation before it can be joined.")
    key_kind = _column_kind(left_non_null)

    def normalize_key(value: Any) -> Any:
        if key_kind == "numeric":
            return _as_decimal(value)
        if key_kind == "date":
            return _date_comparison_value(value)
        return str(value)

    left_key_values = [normalize_key(value) for value in left_values if value is not None and not pd.isna(value)]
    right_key_values = [normalize_key(value) for value in right_values if value is not None and not pd.isna(value)]
    left_unique = len(left_key_values) == len(set(left_key_values))
    right_unique = len(right_key_values) == len(set(right_key_values))
    if not left_unique and not right_unique:
        raise ToolExecutionError("many_to_many_join", "This many-to-many relationship could multiply records unsafely.")
    left_counts: dict[Any, int] = {}
    right_counts: dict[Any, int] = {}
    for value in left_key_values:
        left_counts[value] = left_counts.get(value, 0) + 1
    for value in right_key_values:
        right_counts[value] = right_counts.get(value, 0) + 1
    expected_join_rows = sum(count * right_counts.get(value, 0) for value, count in left_counts.items())
    if expected_join_rows > MAX_JOIN_OUTPUT_ROWS:
        raise ToolExecutionError("join_output_too_large", "The join would exceed the configured output safety limit.")

    left_columns = [_get_column(left_sheet, column_id) for column_id in args.left_columns]
    right_columns = [_get_column(right_sheet, column_id) for column_id in args.right_columns]
    if len(set(args.left_columns)) != len(args.left_columns) or len(set(args.right_columns)) != len(args.right_columns):
        raise ToolExecutionError("invalid_join_columns", "Join output columns must be unique.")
    left_join_key = "__privy_left_join_key__"
    right_join_key = "__privy_right_join_key__"
    left_names = {column.policy_key: f"left:{column.policy_key}" for column in left_columns}
    right_names = {column.policy_key: f"right:{column.policy_key}" for column in right_columns}
    left_data = left_frame[[column.policy_key for column in left_columns]].copy().rename(columns=left_names)
    right_data = right_frame[[column.policy_key for column in right_columns]].copy().rename(columns=right_names)
    left_data[left_join_key] = [normalize_key(value) if value is not None and not pd.isna(value) else None for value in left_values]
    right_data[right_join_key] = [normalize_key(value) if value is not None and not pd.isna(value) else None for value in right_values]
    left_data = left_data[left_data[left_join_key].notna()]
    right_data = right_data[right_data[right_join_key].notna()]
    validate = "one_to_one" if left_unique and right_unique else "one_to_many" if left_unique else "many_to_one"
    try:
        joined = left_data.merge(
            right_data,
            how="inner",
            left_on=left_join_key,
            right_on=right_join_key,
            suffixes=("_left", "_right"),
            validate=validate,
            sort=False,
        )
    except (pd.errors.MergeError, ValueError) as exc:
        raise ToolExecutionError("unsafe_join", "The observed join cardinality failed validation.") from exc
    output_count = len(joined)
    if output_count != expected_join_rows or output_count > max(1, len(left_data), len(right_data)):
        raise ToolExecutionError("join_amplification", "The join would produce excessive duplicate amplification.")
    derived_columns = {
        **{f"left:{column.column_id}": column for column in left_columns},
        **{f"right:{column.column_id}": column for column in right_columns},
    }
    derived_data = {
        f"left:{column.column_id}": joined[left_names[column.policy_key]].reset_index(drop=True)
        for column in left_columns
    }
    derived_data.update({
        f"right:{column.column_id}": joined[right_names[column.policy_key]].reset_index(drop=True)
        for column in right_columns
    })
    derived_frame = pd.DataFrame(derived_data)
    memory_bytes = int(derived_frame.memory_usage(index=True, deep=True).sum())
    if context.join_result_bytes + memory_bytes > MAX_JOIN_MEMORY_BYTES:
        raise ToolExecutionError("join_memory_limit", "The joined result exceeds the request memory limit.")
    result_ref = str(uuid.uuid4())
    source_identities = [
        _source_identity(context, args.left_file_id, args.left_worksheet_id),
        _source_identity(context, args.right_file_id, args.right_worksheet_id),
    ]
    context.join_results[result_ref] = JoinedToolResult(
        result_id=result_ref,
        owner_id=context.user_id,
        chat_id=context.chat_id,
        frame=derived_frame,
        columns=derived_columns,
        sources=source_identities,
        created_at=time.monotonic(),
        memory_bytes=memory_bytes,
        transformations=["join_tables"],
    )
    context.join_result_bytes += memory_bytes
    result_rows = []
    for row_index, (_, row) in enumerate(joined.head(MAX_TOOL_ROWS).iterrows(), start=1):
        values = {}
        for column in left_columns:
            values[f"left:{column.column_id}"] = _safe_value(column, row.get(left_names[column.policy_key]))
        for column in right_columns:
            values[f"right:{column.column_id}"] = _safe_value(column, row.get(right_names[column.policy_key]))
        result_rows.append({"row_index": row_index, "values": values})
    return {
        "exact": True,
        "verification_status": "exact",
        "validation_checks": ["join_keys_compatible", "join_keys_share_permitted", "cardinality_validated", "expected_size_checked", "duplicate_amplification_checked", "source_ownership_checked", "source_policies_checked"],
        "join_type": "inner",
        "cardinality": validate,
        "left_row_count": len(left_frame),
        "right_row_count": len(right_frame),
        "matched_row_count": output_count,
        "expected_matched_row_count": expected_join_rows,
        "left_null_key_count": int(len(left_values) - len(left_key_values)),
        "right_null_key_count": int(len(right_values) - len(right_key_values)),
        "left_unmatched_row_count": sum(count for value, count in left_counts.items() if value not in right_counts),
        "right_unmatched_row_count": sum(count for value, count in right_counts.items() if value not in left_counts),
        "duplicate_amplification_ratio": round(output_count / max(1, len(left_key_values), len(right_key_values)), 6),
        "result_ref": result_ref,
        "returned_row_count": len(result_rows),
        "truncated": output_count > len(result_rows),
        "rows": result_rows,
        "join_sources": [
            {"file_id": args.left_file_id, "worksheet_id": args.left_worksheet_id},
            {"file_id": args.right_file_id, "worksheet_id": args.right_worksheet_id},
        ],
    }


def _aggregate_joined(context: ToolContext, raw_args: BaseModel) -> dict[str, Any]:
    args = raw_args
    assert isinstance(args, JoinedAggregateArgs)
    dataset = context.join_results.get(args.result_ref)
    if dataset is None:
        raise ToolExecutionError("result_not_found", "That intermediate result is unavailable in this request.")
    if dataset.owner_id != context.user_id or dataset.chat_id != context.chat_id:
        raise ToolExecutionError("result_forbidden", "That intermediate result is not available in this conversation.")
    if time.monotonic() - dataset.created_at > 300:
        context.join_results.pop(args.result_ref, None)
        context.join_result_bytes = max(0, context.join_result_bytes - dataset.memory_bytes)
        raise ToolExecutionError("result_expired", "That intermediate result has expired.")
    current_sources = []
    try:
        for source in dataset.sources:
            current_sources.append(_source_identity(context, source["file_id"], source["table_id"]))
    except (KeyError, ToolExecutionError):
        current_sources = []
    if current_sources != dataset.sources:
        context.join_results.pop(args.result_ref, None)
        context.join_result_bytes = max(0, context.join_result_bytes - dataset.memory_bytes)
        raise ToolExecutionError("stale_result", "A source or privacy policy changed; rediscover and rejoin the files.")
    if args.aggregation not in _AGGREGATIONS:
        raise ToolExecutionError("invalid_aggregation", "That aggregation is not supported.")
    column = dataset.columns.get(args.column_ref) if args.column_ref else None
    if args.column_ref and column is None:
        raise ToolExecutionError("column_not_found", "That joined column is unavailable or excluded.")
    if args.aggregation != "count" and column is None:
        raise ToolExecutionError("missing_column", "Select a joined column for this aggregation.")
    values = _non_null(dataset.frame[args.column_ref]) if column else [True] * len(dataset.frame)
    aggregate = _compute_aggregate(values, args.aggregation, column)
    return {
        "result_ref": args.result_ref,
        "aggregation": args.aggregation,
        "column_ref": args.column_ref,
        "row_count": len(dataset.frame),
        "exact": True,
        "source_identities": dataset.sources,
        "prior_transformations": dataset.transformations,
        **aggregate,
    }


def tool_definitions() -> list[ToolDefinition]:
    common = frozenset({"read_workbook"})
    return [
        ToolDefinition(
            name="discover_joins",
            description="Find possible relationships between attached files using permitted column names, types, null rates, and key uniqueness. This only discovers candidates; ambiguous relationships require user confirmation.",
            parameters={"type": "object", "properties": {"file_ids": {"type": "array", "minItems": 2, "maxItems": 8, "items": {"type": "string"}}}, "additionalProperties": False},
            required_permissions=common,
            argument_model=JoinDiscoveryArgs,
            handler=_discover_joins,
            result_schema={"type": "object", "properties": {"candidates": {"type": "array"}, "exact": {"type": "boolean"}}, "required": ["candidates", "exact"]},
            max_result_bytes=MAX_TOOL_RESULT_BYTES,
        ),
        ToolDefinition(
            name="join_tables",
            description="Execute an inner join only for a unique, compatible, Share-permitted key relationship. Ambiguous, excluded, masked-key, many-to-many, oversized, and high-amplification joins are rejected. Returns a bounded privacy-safe sample plus the exact matched count.",
            parameters={
                "type": "object",
                "properties": {
                    "left_file_id": {"type": "string"},
                    "left_worksheet_id": {"type": "string"},
                    "left_key_column_id": {"type": "string"},
                    "right_file_id": {"type": "string"},
                    "right_worksheet_id": {"type": "string"},
                    "right_key_column_id": {"type": "string"},
                    "left_columns": {"type": "array", "minItems": 1, "maxItems": MAX_FILTER_COLUMNS, "items": {"type": "string"}},
                    "right_columns": {"type": "array", "minItems": 1, "maxItems": MAX_FILTER_COLUMNS, "items": {"type": "string"}},
                },
                "required": ["left_file_id", "left_worksheet_id", "left_key_column_id", "right_file_id", "right_worksheet_id", "right_key_column_id", "left_columns", "right_columns"],
                "additionalProperties": False,
            },
            required_permissions=common,
            argument_model=JoinArgs,
            handler=_join_tables,
            result_schema={"type": "object", "properties": {"exact": {"type": "boolean"}, "cardinality": {"type": "string"}, "matched_row_count": {"type": "integer"}, "returned_row_count": {"type": "integer"}, "rows": {"type": "array"}}, "required": ["exact", "cardinality", "matched_row_count", "returned_row_count", "rows"]},
            max_result_bytes=MAX_TOOL_RESULT_BYTES,
        ),
        ToolDefinition(
            name="aggregate_joined_data",
            description="Run an exact bounded aggregate over the complete in-memory result of a prior join in this request. The sample returned by join_tables is not the calculation source.",
            parameters={
                "type": "object",
                "properties": {
                    "result_ref": {"type": "string"},
                    "aggregation": _aggregation_schema(),
                    "column_ref": {"type": ["string", "null"]},
                },
                "required": ["result_ref", "aggregation"],
                "additionalProperties": False,
            },
            required_permissions=common,
            argument_model=JoinedAggregateArgs,
            handler=_aggregate_joined,
            result_schema={"type": "object", "properties": {"result_ref": {"type": "string"}, "exact": {"type": "boolean"}, "row_count": {"type": "integer"}, "value": {"type": ["string", "integer", "number", "null"]}}, "required": ["result_ref", "exact", "row_count", "value"]},
            max_result_bytes=MAX_TOOL_RESULT_BYTES,
        ),
        ToolDefinition(
            name="get_workbook_schema",
            description="Get authorized workbook worksheets, permitted columns, types, row counts, privacy actions, and structural warnings. No cell values are returned.",
            parameters={
                "type": "object",
                "properties": {"file_id": {"type": "string", "description": "Workbook identifier from the attached schema; omit to list all attached workbooks."}},
                "additionalProperties": False,
            },
            required_permissions=common,
            argument_model=SchemaArgs,
            handler=_get_schema,
            result_schema=_RESULT_SCHEMAS["get_workbook_schema"],
            max_result_bytes=MAX_TOOL_RESULT_BYTES,
        ),
        ToolDefinition(
            name="get_column_profile",
            description="Return exact row/null/distinct counts and safe numeric or categorical summaries for one permitted column. Never returns raw sensitive labels.",
            parameters={
                "type": "object",
                "properties": {
                    **_column_schema()["properties"],
                    "category_limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["file_id", "worksheet_id", "column_id"],
                "additionalProperties": False,
            },
            required_permissions=common,
            argument_model=ProfileArgs,
            handler=_get_profile,
            result_schema=_RESULT_SCHEMAS["get_column_profile"],
            max_result_bytes=MAX_TOOL_RESULT_BYTES,
        ),
        ToolDefinition(
            name="filter_rows",
            description="Filter the complete authorized worksheet using bounded AND/OR conditions and return only explicitly requested, privacy-safe columns and a bounded row sample.",
            parameters={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "worksheet_id": {"type": "string"},
                    "conditions": {"type": "array", "minItems": 1, "maxItems": MAX_FILTER_CONDITIONS, "items": {
                        "type": "object",
                        "properties": {
                            "column_id": {"type": "string"},
                            "operator": {"type": "string", "enum": sorted(_FILTER_OPERATORS)},
                            "value": _PRIMITIVE,
                        },
                        "required": ["column_id", "operator"],
                        "additionalProperties": False,
                    }},
                    "match": {"type": "string", "enum": ["all", "any"]},
                    "columns": {"type": "array", "minItems": 1, "maxItems": MAX_FILTER_COLUMNS, "items": {"type": "string"}},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_TOOL_ROWS},
                },
                "required": ["file_id", "worksheet_id", "conditions", "columns"],
                "additionalProperties": False,
            },
            required_permissions=common,
            argument_model=FilterArgs,
            handler=_filter_rows,
            result_schema=_RESULT_SCHEMAS["filter_rows"],
            max_result_bytes=MAX_TOOL_RESULT_BYTES,
        ),
        ToolDefinition(
            name="aggregate_data",
            description="Compute an exact count, sum, average, minimum, maximum, or distinct count over the complete authorized worksheet.",
            parameters={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "worksheet_id": {"type": "string"},
                    "aggregation": _aggregation_schema(),
                    "column_id": {"type": ["string", "null"]},
                },
                "required": ["file_id", "worksheet_id", "aggregation"],
                "additionalProperties": False,
            },
            required_permissions=common,
            argument_model=AggregateArgs,
            handler=_aggregate,
            result_schema=_RESULT_SCHEMAS["aggregate_data"],
            max_result_bytes=MAX_TOOL_RESULT_BYTES,
        ),
        ToolDefinition(
            name="group_and_aggregate",
            description="Group the complete authorized worksheet by a permitted categorical column and apply one approved aggregate, with a bounded group count.",
            parameters={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "worksheet_id": {"type": "string"},
                    "group_column_id": {"type": "string"},
                    "aggregation": _aggregation_schema(),
                    "metric_column_id": {"type": ["string", "null"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_TOOL_GROUPS},
                },
                "required": ["file_id", "worksheet_id", "group_column_id", "aggregation"],
                "additionalProperties": False,
            },
            required_permissions=common,
            argument_model=GroupAggregateArgs,
            handler=_group_aggregate,
            result_schema=_RESULT_SCHEMAS["group_and_aggregate"],
            max_result_bytes=MAX_TOOL_RESULT_BYTES,
        ),
        ToolDefinition(
            name="compare_groups",
            description="Compare two to five explicitly selected groups using an approved exact aggregate over the complete authorized worksheet.",
            parameters={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "worksheet_id": {"type": "string"},
                    "group_column_id": {"type": "string"},
                    "group_values": {"type": "array", "minItems": 2, "maxItems": 5, "items": {"type": "string"}},
                    "aggregation": _aggregation_schema(),
                    "metric_column_id": {"type": ["string", "null"]},
                },
                "required": ["file_id", "worksheet_id", "group_column_id", "group_values", "aggregation"],
                "additionalProperties": False,
            },
            required_permissions=common,
            argument_model=CompareGroupsArgs,
            handler=_compare_groups,
            result_schema=_RESULT_SCHEMAS["compare_groups"],
            max_result_bytes=MAX_TOOL_RESULT_BYTES,
        ),
    ]


def _get_schema(context: ToolContext, args: SchemaArgs) -> dict[str, Any]:
    if args.file_id is not None:
        return _available_schema(_get_workbook(context, args.file_id))
    return {"workbooks": [_available_schema(_load_tool_workbook(context, file_id)) for file_id in context.files]}


def _column_for_tool(context: ToolContext, args: ColumnArgs) -> tuple[ToolWorksheet, ToolColumn, pd.DataFrame]:
    sheet = _get_sheet(context, args.file_id, args.worksheet_id)
    column = _get_column(sheet, args.column_id)
    frame = _ensure_frame(context, args.file_id, sheet)
    return sheet, column, frame


def _series(sheet: ToolWorksheet, column: ToolColumn, frame: pd.DataFrame) -> pd.Series:
    return frame[column.policy_key]


def _non_null(values: pd.Series) -> list[Any]:
    return [value for value in values.tolist() if value is not None and not pd.isna(value)]


def _as_decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise InvalidOperation
    try:
        text = str(value).strip()
        if len(text) > 128:
            raise InvalidOperation
        number = Decimal(text)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise InvalidOperation from exc
    if not number.is_finite():
        raise InvalidOperation
    return number


def _parse_iso_date(value: Any) -> date | datetime:
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text)
            return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError as exc:
            raise ToolExecutionError("invalid_date", "Date values could not be safely interpreted.") from exc


def _date_comparison_value(value: Any) -> datetime:
    parsed = _parse_iso_date(value)
    if isinstance(parsed, datetime):
        return parsed
    return datetime.combine(parsed, datetime.min.time())


def _column_kind(values: list[Any]) -> str:
    if not values:
        return "empty"
    try:
        for value in values:
            _as_decimal(value)
        return "numeric"
    except InvalidOperation:
        pass
    try:
        for value in values:
            _parse_iso_date(value)
        return "date"
    except ToolExecutionError:
        pass
    if all(isinstance(value, str) for value in values):
        return "categorical"
    return "mixed"


def _safe_value(column: ToolColumn, value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    if column.protected or scan_for_unmasked_pii(str(value)):
        return _MASKED if column.action == "mask" else _REDACTED
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "item"):
        value = value.item()
    return value if isinstance(value, (str, int, float, bool)) else str(value)


def _compute_aggregate(
    values: list[Any],
    aggregation: str,
    column: ToolColumn | None,
) -> dict[str, Any]:
    if aggregation not in _AGGREGATIONS:
        raise ToolExecutionError("invalid_aggregation", "That aggregation is not supported.")
    if aggregation == "count":
        return {"value": len(values), "non_null_count": len(values)}
    if column is None:
        raise ToolExecutionError("missing_column", "This aggregation requires a column.")
    if column.action == "mask" and aggregation not in {"count", "distinct_count"}:
        raise ToolExecutionError("masked_column", "This operation is not available for a masked column.")
    if column.sensitive and aggregation not in {"count", "distinct_count"}:
        raise ToolExecutionError("sensitive_column", "This operation is not available for a sensitive column.")
    if aggregation == "distinct_count":
        return {"value": len({str(value) for value in values}), "non_null_count": len(values)}
    if aggregation in {"sum", "average", "minimum", "maximum"}:
        try:
            numeric_values = [_as_decimal(value) for value in values]
        except InvalidOperation as exc:
            if aggregation in {"minimum", "maximum"} and _column_kind(values) == "date":
                date_values = [(_date_comparison_value(value), _parse_iso_date(value)) for value in values]
                selected = (
                    min(date_values, key=lambda item: item[0])[1]
                    if aggregation == "minimum"
                    else max(date_values, key=lambda item: item[0])[1]
                )
                return {
                    "value": selected.isoformat(),
                    "non_null_count": len(date_values),
                    "result_type": "date",
                }
            raise ToolExecutionError("non_numeric_column", "The selected column contains values that are not all numeric.") from exc
        if not numeric_values:
            return {"value": None, "non_null_count": 0}
        precision = max(
            38,
            max((len(value.as_tuple().digits) for value in numeric_values), default=1)
            + len(str(len(numeric_values)))
            + 8,
        )
        with localcontext() as decimal_context:
            decimal_context.prec = precision
            if aggregation == "sum":
                value = sum(numeric_values, Decimal(0))
            elif aggregation == "average":
                value = sum(numeric_values, Decimal(0)) / Decimal(len(numeric_values))
            elif aggregation == "minimum":
                value = min(numeric_values)
            else:
                value = max(numeric_values)
        return {"value": format(value, "f"), "non_null_count": len(numeric_values), "numeric_precision": "decimal"}
    raise ToolExecutionError("invalid_aggregation", "That aggregation is not supported.")


def _check_match(operator: str, actual: Any, expected: Any, kind: str) -> bool:
    if operator == "is_null":
        return actual is None or pd.isna(actual)
    if operator == "not_null":
        return actual is not None and not pd.isna(actual)
    if actual is None or pd.isna(actual):
        return False
    if operator in {"contains", "starts_with"}:
        if not isinstance(actual, str) or not isinstance(expected, str):
            raise ToolExecutionError("invalid_filter", "Text filters require text values.")
        return expected.casefold() in actual.casefold() if operator == "contains" else actual.casefold().startswith(expected.casefold())
    if operator in {"gt", "gte", "lt", "lte"}:
        if kind == "numeric":
            left, right = _as_decimal(actual), _as_decimal(expected)
        elif kind == "date":
            left, right = _date_comparison_value(actual), _date_comparison_value(expected)
        else:
            raise ToolExecutionError("invalid_filter", "Ordered comparisons require a consistently numeric or date column.")
        return {
            "gt": left > right,
            "gte": left >= right,
            "lt": left < right,
            "lte": left <= right,
        }[operator]
    if operator in {"eq", "ne"}:
        if kind == "numeric":
            equal = _as_decimal(actual) == _as_decimal(expected)
        elif kind == "date":
            equal = _date_comparison_value(actual) == _date_comparison_value(expected)
        else:
            equal = str(actual).casefold() == str(expected).casefold()
        return equal if operator == "eq" else not equal
    raise ToolExecutionError("invalid_filter", "That filter operator is not supported.")


def _get_profile(context: ToolContext, raw_args: BaseModel) -> dict[str, Any]:
    args = raw_args
    assert isinstance(args, ProfileArgs)
    sheet, column, frame = _column_for_tool(context, args)
    values = _non_null(_series(sheet, column, frame))
    kind = _column_kind(values)
    result: dict[str, Any] = {
        "workbook_id": args.file_id,
        "worksheet_id": sheet.worksheet_id,
        "column_id": column.column_id,
        "data_type": column.data_type or kind,
        "total_rows": sheet.row_count,
        "non_null_count": len(values),
        "null_count": sheet.row_count - len(values),
        "distinct_count": len({str(value) for value in values}),
        "exact": True,
    }
    if not column.protected and kind == "numeric":
        numeric = [_as_decimal(value) for value in values]
        if numeric:
            result["minimum"] = format(min(numeric), "f")
            result["maximum"] = format(max(numeric), "f")
    elif not column.protected and kind == "categorical":
        counts: dict[str, int] = {}
        for value in values:
            text = str(value)
            if not scan_for_unmasked_pii(text):
                counts[text] = counts.get(text, 0) + 1
        result["top_categories"] = [
            {"value": value, "count": count}
            for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:args.category_limit]
        ]
    return result


def _filter_rows(context: ToolContext, raw_args: BaseModel) -> dict[str, Any]:
    args = raw_args
    assert isinstance(args, FilterArgs)
    if args.match not in {"all", "any"}:
        raise ToolExecutionError("invalid_filter", "The filter combination must be 'all' or 'any'.")
    if len(set(args.columns)) != len(args.columns):
        raise ToolExecutionError("invalid_filter", "Requested columns must be unique.")
    sheet = _get_sheet(context, args.file_id, args.worksheet_id)
    result_columns = [_get_column(sheet, column_id) for column_id in args.columns]
    conditions: list[tuple[ToolColumn, FilterCondition, str]] = []
    for condition in args.conditions:
        if condition.operator not in _FILTER_OPERATORS:
            raise ToolExecutionError("invalid_filter", "That filter operator is not supported.")
        if condition.operator in {"is_null", "not_null"} and condition.value is not None:
            raise ToolExecutionError("invalid_filter", "Null checks do not accept a value.")
        if condition.operator not in {"is_null", "not_null"} and condition.value is None:
            raise ToolExecutionError("invalid_filter", "This filter requires a comparison value.")
        if isinstance(condition.value, str) and len(condition.value) > 512:
            raise ToolExecutionError("invalid_filter", "Filter values must be 512 characters or fewer.")
        column = _get_column(sheet, condition.column_id)
        frame = _ensure_frame(context, args.file_id, sheet)
        values = _non_null(_series(sheet, column, frame))
        conditions.append((column, condition, _column_kind(values)))
    frame = _ensure_frame(context, args.file_id, sheet)
    matches = []
    for row_index, row in frame.iterrows():
        row_matches = [
            _check_match(
                condition.operator,
                row.get(column.policy_key),
                condition.value,
                kind,
            )
            for column, condition, kind in conditions
        ]
        if all(row_matches) if args.match == "all" else any(row_matches):
            matches.append((row_index, row))
    rows = []
    for index, row in matches[:args.limit]:
        rows.append({
            "row_index": int(index) + 1,
            "values": {
                column.column_id: _safe_value(column, row.get(column.policy_key))
                for column in result_columns
            },
        })
    return {
        "workbook_id": args.file_id,
        "worksheet_id": sheet.worksheet_id,
        "exact": True,
        "matched_row_count": len(matches),
        "returned_row_count": len(rows),
        "truncated": len(matches) > len(rows),
        "rows": rows,
    }


def _aggregate_handler(context: ToolContext, args: AggregateArgs) -> dict[str, Any]:
    if args.aggregation not in _AGGREGATIONS:
        raise ToolExecutionError("invalid_aggregation", "That aggregation is not supported.")
    sheet = _get_sheet(context, args.file_id, args.worksheet_id)
    column = _get_column(sheet, args.column_id) if args.column_id else None
    frame = _ensure_frame(context, args.file_id, sheet)
    values = _non_null(_series(sheet, column, frame)) if column else [True] * sheet.row_count
    aggregation = _compute_aggregate(values, args.aggregation, column)
    return {
        "workbook_id": args.file_id,
        "worksheet_id": sheet.worksheet_id,
        "aggregation": args.aggregation,
        "column_id": column.column_id if column else None,
        "row_count": sheet.row_count,
        "exact": True,
        **aggregation,
    }


def _aggregate(context: ToolContext, raw_args: BaseModel) -> dict[str, Any]:
    assert isinstance(raw_args, AggregateArgs)
    if raw_args.aggregation != "count" and raw_args.column_id is None:
        raise ToolExecutionError("missing_column", "Select a column for this aggregation.")
    return _aggregate_handler(context, raw_args)


def _group_aggregate_values(
    sheet: ToolWorksheet,
    group_column: ToolColumn,
    metric_column: ToolColumn | None,
    frame: pd.DataFrame,
    aggregation: str,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    if aggregation not in _AGGREGATIONS:
        raise ToolExecutionError("invalid_aggregation", "That aggregation is not supported.")
    if aggregation != "count" and metric_column is None:
        raise ToolExecutionError("missing_column", "Select a metric column for this aggregation.")
    groups: dict[tuple[bool, str], list[Any]] = {}
    group_series = _series(sheet, group_column, frame)
    metric_series = _series(sheet, metric_column, frame) if metric_column else None
    for index, value in group_series.items():
        is_null = value is None or pd.isna(value)
        key = (is_null, "" if is_null else str(value))
        groups.setdefault(key, []).append(metric_series.loc[index] if metric_series is not None else True)
        if len(groups) > MAX_TOOL_GROUPS:
            raise ToolExecutionError("too_many_groups", f"The worksheet has more than {MAX_TOOL_GROUPS} groups; choose a lower-cardinality column.")
    result = []
    for group_index, ((is_null, group_value), values) in enumerate(
        sorted(groups.items(), key=lambda item: item[0])[:limit],
        start=1,
    ):
        non_null_values = [value for value in values if value is not None and not pd.isna(value)]
        aggregate = _compute_aggregate(non_null_values, aggregation, metric_column)
        label = "<NULL>" if is_null else group_value
        if group_column.protected or scan_for_unmasked_pii(label):
            label = f"Group {group_index}"
        result.append({
            "group": label,
            "row_count": len(values),
            **aggregate,
        })
    return result, len(groups)


def _group_aggregate(context: ToolContext, raw_args: BaseModel) -> dict[str, Any]:
    args = raw_args
    assert isinstance(args, GroupAggregateArgs)
    sheet = _get_sheet(context, args.file_id, args.worksheet_id)
    group_column = _get_column(sheet, args.group_column_id)
    metric_column = _get_column(sheet, args.metric_column_id) if args.metric_column_id else None
    frame = _ensure_frame(context, args.file_id, sheet)
    groups, group_count = _group_aggregate_values(
        sheet, group_column, metric_column, frame, args.aggregation, args.limit
    )
    return {
        "workbook_id": args.file_id,
        "worksheet_id": sheet.worksheet_id,
        "aggregation": args.aggregation,
        "row_count": sheet.row_count,
        "group_count": group_count,
        "exact": True,
        "groups": groups,
    }


def _compare_groups(context: ToolContext, raw_args: BaseModel) -> dict[str, Any]:
    args = raw_args
    assert isinstance(args, CompareGroupsArgs)
    if len(set(args.group_values)) != len(args.group_values):
        raise ToolExecutionError("invalid_groups", "Comparison groups must be unique.")
    if any(len(value) > 256 for value in args.group_values):
        raise ToolExecutionError("invalid_groups", "Comparison group values must be 256 characters or fewer.")
    sheet = _get_sheet(context, args.file_id, args.worksheet_id)
    group_column = _get_column(sheet, args.group_column_id)
    metric_column = _get_column(sheet, args.metric_column_id) if args.metric_column_id else None
    frame = _ensure_frame(context, args.file_id, sheet)
    group_series = _series(sheet, group_column, frame)
    metric_series = _series(sheet, metric_column, frame) if metric_column else None
    comparisons = []
    for group_index, expected in enumerate(args.group_values, start=1):
        indices = [
            index for index, value in group_series.items()
            if value is not None and not pd.isna(value) and str(value).casefold() == expected.casefold()
        ]
        values = (
            [metric_series.loc[index] for index in indices]
            if metric_series is not None
            else [True] * len(indices)
        )
        non_null_values = [value for value in values if value is not None and not pd.isna(value)]
        aggregate = _compute_aggregate(non_null_values, args.aggregation, metric_column)
        comparisons.append({
            "group": (
                f"Group {group_index}"
                if group_column.protected or scan_for_unmasked_pii(expected)
                else expected
            ),
            "row_count": len(indices),
            **aggregate,
        })
    return {
        "workbook_id": args.file_id,
        "worksheet_id": sheet.worksheet_id,
        "aggregation": args.aggregation,
        "row_count": sheet.row_count,
        "exact": True,
        "comparisons": comparisons,
    }


_TOOL_REGISTRY = {item.name: item for item in tool_definitions()}


def provider_tool_schemas() -> list[dict[str, Any]]:
    return [tool.provider_schema() for tool in _TOOL_REGISTRY.values()]


def execute_tool(context: ToolContext, name: str, arguments: str | dict[str, Any]) -> dict[str, Any]:
    if not store.get_chat(context.chat_id, context.user_id):
        raise ToolExecutionError("chat_not_found", "The requested chat is not available.")
    context.execution_count += 1
    if context.execution_count > MAX_TOOL_CALLS_PER_REQUEST:
        raise ToolExecutionError("tool_call_limit", "This request reached its workbook tool-call limit.")
    definition = _TOOL_REGISTRY.get(name)
    if definition is None:
        raise ToolExecutionError("unknown_tool", "That workbook operation is not available.")
    if "read_workbook" not in definition.required_permissions:
        raise ToolExecutionError("permission_denied", "This workbook operation is not permitted.")
    if isinstance(arguments, str):
        if len(arguments.encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
            raise ToolExecutionError("arguments_too_large", "Tool arguments exceeded the allowed size.")
        try:
            arguments = json.loads(arguments)
        except ValueError as exc:
            raise ToolExecutionError("invalid_arguments", "The tool arguments were not valid JSON.") from exc
    if not isinstance(arguments, dict):
        raise ToolExecutionError("invalid_arguments", "Tool arguments must be a JSON object.")
    if len(json.dumps(arguments, ensure_ascii=True).encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
        raise ToolExecutionError("arguments_too_large", "Tool arguments exceeded the allowed size.")
    if isinstance(arguments.get("file_ids"), list):
        if any(not isinstance(file_id, str) for file_id in arguments["file_ids"]):
            raise ToolExecutionError("invalid_arguments", "File identifiers must be strings.")
        _refresh_authorized_files(context, None)
        if any(file_id not in context.files for file_id in arguments["file_ids"]):
            raise ToolExecutionError("file_not_found", "One or more requested files are not attached to this chat.")
    elif name == "join_tables":
        source_ids = [arguments.get("left_file_id"), arguments.get("right_file_id")]
        if any(not isinstance(file_id, str) for file_id in source_ids):
            raise ToolExecutionError("invalid_arguments", "Join file identifiers must be strings.")
        _refresh_authorized_files(context, None)
        if any(file_id not in context.files for file_id in source_ids):
            raise ToolExecutionError("file_not_found", "One or more requested files are not attached to this chat.")
    else:
        file_id = arguments.get("file_id")
        if file_id is not None and not isinstance(file_id, str):
            raise ToolExecutionError("invalid_arguments", "The file identifier must be a string.")
        _refresh_authorized_files(context, file_id)
    started = time.monotonic()
    result: dict[str, Any] | None = None
    try:
        validated = definition.argument_model.model_validate(arguments)
        result = definition.handler(context, validated)
        result = _attach_result_contract(context, name, arguments, result)
    except ValidationError as exc:
        raise ToolExecutionError("invalid_arguments", "The tool arguments did not match the operation schema.") from exc
    except HTTPException as exc:
        raise ToolExecutionError(
            "privacy_validation_failed",
            "The workbook could not pass its privacy and integrity checks.",
        ) from exc
    if time.monotonic() - started > MAX_TOOL_EXECUTION_SECONDS:
        _discard_join_result(context, result.get("result_ref") if result else None)
        raise ToolExecutionError("execution_timeout", "The workbook operation exceeded its execution time limit.")
    result.setdefault("execution_metadata", {})["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
    encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > min(MAX_TOOL_RESULT_BYTES, definition.max_result_bytes):
        _discard_join_result(context, result.get("result_ref"))
        raise ToolExecutionError("result_too_large", "The workbook result exceeded the allowed size.")
    if scan_for_unmasked_pii(encoded):
        _discard_join_result(context, result.get("result_ref"))
        raise ToolExecutionError("privacy_validation_failed", "The workbook result did not pass outbound privacy validation.")
    return result


def execute_tool_safely(context: ToolContext, name: str, arguments: str | dict[str, Any]) -> dict[str, Any]:
    try:
        result = execute_tool(context, name, arguments)
        return {"ok": True, "result": result}
    except ToolExecutionError as exc:
        return {"ok": False, "error": {"code": exc.code, "message": exc.safe_message}}
