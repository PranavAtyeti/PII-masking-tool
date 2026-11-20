"""Bounded, policy-aware local tools for workbook discovery and calculation."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
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
) -> ToolContext:
    if not store.get_chat(chat_id, user_id):
        raise ToolExecutionError("chat_not_found", "The requested chat is not available.")
    if files is None:
        files = store.get_chat_files(chat_id)
    context = ToolContext(
        user_id=user_id,
        chat_id=chat_id,
        files={str(item["file_id"]): item for item in files},
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


def tool_definitions() -> list[ToolDefinition]:
    common = frozenset({"read_workbook"})
    return [
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
    _refresh_authorized_files(context, arguments.get("file_id"))
    started = time.monotonic()
    try:
        validated = definition.argument_model.model_validate(arguments)
        result = definition.handler(context, validated)
    except ValidationError as exc:
        raise ToolExecutionError("invalid_arguments", "The tool arguments did not match the operation schema.") from exc
    except HTTPException as exc:
        raise ToolExecutionError(
            "privacy_validation_failed",
            "The workbook could not pass its privacy and integrity checks.",
        ) from exc
    if time.monotonic() - started > MAX_TOOL_EXECUTION_SECONDS:
        raise ToolExecutionError("execution_timeout", "The workbook operation exceeded its execution time limit.")
    encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > min(MAX_TOOL_RESULT_BYTES, definition.max_result_bytes):
        raise ToolExecutionError("result_too_large", "The workbook result exceeded the allowed size.")
    if scan_for_unmasked_pii(encoded):
        raise ToolExecutionError("privacy_validation_failed", "The workbook result did not pass outbound privacy validation.")
    return result


def execute_tool_safely(context: ToolContext, name: str, arguments: str | dict[str, Any]) -> dict[str, Any]:
    try:
        result = execute_tool(context, name, arguments)
        return {"ok": True, "result": result}
    except ToolExecutionError as exc:
        return {"ok": False, "error": {"code": exc.code, "message": exc.safe_message}}
