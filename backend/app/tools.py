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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
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
MAX_ANALYSIS_PLAN_STEPS = max(1, min(int(os.getenv("PRIVY_MAX_ANALYSIS_PLAN_STEPS", "5")), 5))
MAX_ANALYSIS_PLAN_DEPTH = max(1, min(int(os.getenv("PRIVY_MAX_ANALYSIS_PLAN_DEPTH", "3")), 3))
MAX_ANALYSIS_PLAN_SECONDS = max(1.0, min(float(os.getenv("PRIVY_MAX_ANALYSIS_PLAN_SECONDS", "30")), 30.0))
MAX_ANALYSIS_GROUP_CARDINALITY = max(20, min(int(os.getenv("PRIVY_MAX_ANALYSIS_GROUP_CARDINALITY", "5000")), 5000))


def _new_result_ref() -> str:
    """Return an opaque high-entropy handle that cannot resemble numeric PII."""
    return uuid.uuid4().hex.translate(str.maketrans("0123456789", "abcdefghij"))

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
            "workbook_id": {"type": ["string", "null"]},
            "worksheet_id": {"type": ["string", "null"]},
            "exact": {"type": "boolean"},
            "matched_row_count": {"type": "integer"},
            "returned_row_count": {"type": "integer"},
            "truncated": {"type": "boolean"},
            "result_ref": {"type": "string"},
            "rows": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["exact", "matched_row_count", "returned_row_count", "truncated", "result_ref", "rows"],
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
            "workbook_id": {"type": ["string", "null"]},
            "worksheet_id": {"type": ["string", "null"]},
            "aggregation": {"type": "string"},
            "row_count": {"type": "integer"},
            "group_count": {"type": "integer"},
            "exact": {"type": "boolean"},
            "groups": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["aggregation", "row_count", "group_count", "exact", "groups"],
    },
    "compare_groups": {
        "type": "object",
        "properties": {
            "workbook_id": {"type": ["string", "null"]},
            "worksheet_id": {"type": ["string", "null"]},
            "aggregation": {"type": "string"},
            "row_count": {"type": "integer"},
            "exact": {"type": "boolean"},
            "comparisons": {"type": "array", "items": {"type": "object"}},
            "absolute_difference": {"type": ["string", "null"]},
            "percentage_change": {"type": ["string", "null"]},
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
    column_id: str = Field(min_length=1, max_length=160)
    operator: str
    value: str | int | float | bool | None = None


class SortOrder(_StrictArgs):
    column_id: str = Field(min_length=1, max_length=160)
    direction: str = "ascending"


class FilterArgs(_StrictArgs):
    file_id: str | None = Field(default=None, min_length=1, max_length=64)
    worksheet_id: str | None = Field(default=None, min_length=1, max_length=64)
    result_ref: str | None = Field(default=None, min_length=32, max_length=32)
    conditions: list[FilterCondition] = Field(min_length=1, max_length=MAX_FILTER_CONDITIONS)
    match: str = "all"
    columns: list[str] = Field(min_length=1, max_length=MAX_FILTER_COLUMNS)
    limit: int = Field(default=MAX_TOOL_ROWS, ge=1, le=MAX_TOOL_ROWS)
    sort_by: list[SortOrder] = Field(default_factory=list, max_length=3)


class AggregateArgs(_StrictArgs):
    file_id: str = Field(min_length=1, max_length=64)
    worksheet_id: str = Field(min_length=1, max_length=64)
    aggregation: str
    column_id: str | None = Field(default=None, max_length=64)


class GroupMetric(_StrictArgs):
    alias: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    aggregation: str
    metric_column_id: str | None = Field(default=None, max_length=160)


class GroupAggregateArgs(_StrictArgs):
    file_id: str | None = Field(default=None, min_length=1, max_length=64)
    worksheet_id: str | None = Field(default=None, min_length=1, max_length=64)
    result_ref: str | None = Field(default=None, min_length=32, max_length=32)
    group_column_id: str | None = Field(default=None, min_length=1, max_length=160)
    group_column_ids: list[str] | None = Field(default=None, min_length=1, max_length=3)
    aggregation: str
    metric_column_id: str | None = Field(default=None, max_length=160)
    limit: int = Field(default=MAX_TOOL_GROUPS, ge=1, le=MAX_TOOL_GROUPS)
    additional_aggregations: list[GroupMetric] = Field(default_factory=list, max_length=4)
    sort_direction: str = "none"
    sort_metric: str = "primary"
    rank_method: str = "first"
    include_ties: bool = False


class DatasetProfileArgs(_StrictArgs):
    file_id: str = Field(min_length=1, max_length=64)
    worksheet_id: str = Field(min_length=1, max_length=64)
    include_outliers: bool = True


class TrendArgs(_StrictArgs):
    file_id: str = Field(min_length=1, max_length=64)
    worksheet_id: str = Field(min_length=1, max_length=64)
    date_column_id: str = Field(min_length=1, max_length=64)
    interval: str
    aggregation: str
    metric_column_id: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=MAX_TOOL_GROUPS, ge=1, le=MAX_TOOL_GROUPS)


class DerivedColumnArgs(_StrictArgs):
    file_id: str | None = Field(default=None, min_length=1, max_length=64)
    worksheet_id: str | None = Field(default=None, min_length=1, max_length=64)
    result_ref: str | None = Field(default=None, min_length=32, max_length=32)
    operation: str
    left_column_id: str = Field(min_length=1, max_length=160)
    right_column_id: str = Field(min_length=1, max_length=160)
    alias: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")


class CompareGroupsArgs(_StrictArgs):
    file_id: str | None = Field(default=None, min_length=1, max_length=64)
    worksheet_id: str | None = Field(default=None, min_length=1, max_length=64)
    result_ref: str | None = Field(default=None, min_length=32, max_length=32)
    group_column_id: str = Field(min_length=1, max_length=160)
    group_values: list[str] = Field(min_length=2, max_length=5)
    aggregation: str
    metric_column_id: str | None = Field(default=None, max_length=160)
    include_change: bool = False


class JoinDiscoveryArgs(_StrictArgs):
    file_ids: list[str] | None = Field(default=None, min_length=2, max_length=8)


class JoinArgs(_StrictArgs):
    left_file_id: str | None = Field(default=None, min_length=1, max_length=64)
    left_worksheet_id: str | None = Field(default=None, min_length=1, max_length=64)
    left_key_column_id: str | None = Field(default=None, min_length=1, max_length=64)
    left_result_ref: str | None = Field(default=None, min_length=32, max_length=32)
    left_key_column_ref: str | None = Field(default=None, max_length=160)
    right_file_id: str = Field(min_length=1, max_length=64)
    right_worksheet_id: str = Field(min_length=1, max_length=64)
    right_key_column_id: str = Field(min_length=1, max_length=64)
    left_columns: list[str] = Field(min_length=1, max_length=MAX_FILTER_COLUMNS)
    right_columns: list[str] = Field(min_length=1, max_length=MAX_FILTER_COLUMNS)


class JoinedAggregateArgs(_StrictArgs):
    result_ref: str = Field(min_length=32, max_length=32)
    aggregation: str
    column_ref: str | None = Field(default=None, max_length=140)


class AnalysisPlanStep(_StrictArgs):
    step_id: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    capability: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any]
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_ANALYSIS_PLAN_STEPS)


class AnalysisPlanArgs(_StrictArgs):
    objective: str = Field(min_length=1, max_length=512)
    steps: list[AnalysisPlanStep] = Field(min_length=1, max_length=MAX_ANALYSIS_PLAN_STEPS)


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
    exact: bool = True
    warnings: list[str] = field(default_factory=list)


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
    unique_identities = []
    for identity in identities:
        if identity not in unique_identities:
            unique_identities.append(identity)
    identities = unique_identities
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
            "join_keys": result.get("join_keys") or ([
                {"file_id": arguments.get("left_file_id"), "worksheet_id": arguments.get("left_worksheet_id"), "column_id": arguments.get("left_key_column_id")},
                {"file_id": arguments.get("right_file_id"), "worksheet_id": arguments.get("right_worksheet_id"), "column_id": arguments.get("right_key_column_id")},
            ] if operation == "join_tables" else []),
            "sort": [
                {"column_id": item.get("column_id"), "direction": item.get("direction")}
                for item in arguments.get("sort_by", []) if isinstance(item, dict)
            ],
            "calculations": ([{
                "operation": arguments.get("operation"),
                "left_column_id": arguments.get("left_column_id"),
                "right_column_id": arguments.get("right_column_id"),
                "derived_column_ref": result.get("column_ref"),
            }] if operation == "calculate_columns" else []),
            "grouping": ([{
                "group_column_id": arguments.get("group_column_id"),
                "group_column_ids": arguments.get("group_column_ids"),
                "aggregation": arguments.get("aggregation"),
                "metric_column_id": arguments.get("metric_column_id"),
                "additional_aggregations": arguments.get("additional_aggregations", []),
                "sort_direction": arguments.get("sort_direction", "none"),
            }] if operation == "group_and_aggregate" else []),
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
        "partial_result": bool(result.get("partial_result", False) or result.get("truncated", False)),
        "limitations": list(dict.fromkeys([
            *result.get("limitations", []),
            *( ["Result rows were capped by the configured tool limit."] if result.get("truncated") else [] ),
        ])),
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

    def capability(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": 1,
            "supported_operations": [self.name],
            "input_contract": self.argument_model.model_json_schema(),
            "output_contract": self.result_schema,
            "required_permissions": sorted(self.required_permissions),
            "supported_source_types": ["csv", "xls", "xlsx", "docx_table"],
            "resource_requirements": {"bounded": True, "max_result_bytes": self.max_result_bytes,
                                       "max_intermediate_memory_bytes": MAX_JOIN_MEMORY_BYTES,
                                       "max_source_rows": MAX_JOIN_SOURCE_ROWS, "max_output_rows": MAX_JOIN_OUTPUT_ROWS},
            "execution_limits": {"calls_per_request": MAX_TOOL_CALLS_PER_REQUEST, "rounds": MAX_TOOL_ROUNDS,
                                 "plan_steps": MAX_ANALYSIS_PLAN_STEPS, "plan_dependency_depth": MAX_ANALYSIS_PLAN_DEPTH,
                                 "plan_seconds": MAX_ANALYSIS_PLAN_SECONDS, "tool_seconds": MAX_TOOL_EXECUTION_SECONDS},
            "confirmation_required": self.name in {"join_tables", "discover_joins"},
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
    if args.left_result_ref:
        return _join_prior_result(context, args)
    if not args.left_file_id or not args.left_worksheet_id or not args.left_key_column_id:
        raise ToolExecutionError("invalid_sources", "A join needs a source table or a current joined result on its left side.")
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
    result_ref = _new_result_ref()
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


def _validated_join_result(context: ToolContext, result_ref: str) -> JoinedToolResult:
    dataset = context.join_results.get(result_ref)
    if dataset is None:
        raise ToolExecutionError("result_not_found", "That intermediate result is unavailable in this request.")
    if dataset.owner_id != context.user_id or dataset.chat_id != context.chat_id:
        raise ToolExecutionError("result_forbidden", "That intermediate result is not available in this conversation.")
    if time.monotonic() - dataset.created_at > 300:
        _discard_join_result(context, result_ref)
        raise ToolExecutionError("result_expired", "That intermediate result has expired.")
    try:
        current = [_source_identity(context, source["file_id"], source["table_id"]) for source in dataset.sources]
    except (KeyError, ToolExecutionError):
        current = []
    if not current or current != dataset.sources:
        _discard_join_result(context, result_ref)
        raise ToolExecutionError("stale_result", "A source or privacy policy changed; rediscover and rejoin the files.")
    return dataset


def _join_prior_result(context: ToolContext, args: JoinArgs) -> dict[str, Any]:
    """Append one authorized source to a complete request-scoped joined result."""
    if not args.left_result_ref or not args.left_key_column_ref:
        raise ToolExecutionError("invalid_sources", "A prior result join needs its result and key column references.")
    if not args.right_file_id or not args.right_worksheet_id or not args.right_key_column_id:
        raise ToolExecutionError("invalid_sources", "A prior result join needs an authorized right source table.")
    dataset = _validated_join_result(context, args.left_result_ref)
    if any(source.get("file_id") == args.right_file_id for source in dataset.sources):
        raise ToolExecutionError("invalid_sources", "A source file can appear only once in a joined result.")
    right_sheet = _get_sheet(context, args.right_file_id, args.right_worksheet_id)
    right_key = _get_column(right_sheet, args.right_key_column_id)
    left_key = dataset.columns.get(args.left_key_column_ref)
    if left_key is None or args.left_key_column_ref not in dataset.frame:
        raise ToolExecutionError("column_not_found", "The prior result key column is unavailable.")
    if left_key.action != "share" or right_key.action != "share":
        raise ToolExecutionError("ineligible_join_key", "Join keys must be explicitly Share-permitted columns.")
    left_matches = [column for column in dataset.columns.values()
                    if re.sub(r"[^a-z0-9]+", "", column.name.casefold()) == re.sub(r"[^a-z0-9]+", "", right_key.name.casefold())]
    right_matches = [column for column in right_sheet.columns
                     if column.action == "share" and re.sub(r"[^a-z0-9]+", "", column.name.casefold()) == re.sub(r"[^a-z0-9]+", "", left_key.name.casefold())]
    if len(left_matches) != 1 or len(right_matches) != 1 or left_matches[0].column_id != left_key.column_id or right_matches[0].column_id != right_key.column_id:
        raise ToolExecutionError("ambiguous_join", "This chained relationship is ambiguous; select a unique Share-permitted key relationship.")
    right_frame = _ensure_frame(context, args.right_file_id, right_sheet)
    if len(dataset.frame) > MAX_JOIN_SOURCE_ROWS or len(right_frame) > MAX_JOIN_SOURCE_ROWS:
        raise ToolExecutionError("join_source_too_large", "Join sources exceed the configured row safety limit.")
    left_values = dataset.frame[args.left_key_column_ref].tolist()
    right_values = _series(right_sheet, right_key, right_frame).tolist()
    left_non_null, right_non_null = _non_null(pd.Series(left_values)), _non_null(pd.Series(right_values))
    if not left_non_null or not right_non_null or _column_kind(left_non_null) != _column_kind(right_non_null):
        raise ToolExecutionError("incompatible_join_keys", "Join keys have no compatible, non-null values.")
    kind = _column_kind(left_non_null)
    def normalize(value: Any) -> Any:
        if kind == "numeric": return _as_decimal(value)
        if kind == "date": return _date_comparison_value(value)
        return str(value)
    left_keys = [normalize(value) for value in left_values if value is not None and not pd.isna(value)]
    right_keys = [normalize(value) for value in right_values if value is not None and not pd.isna(value)]
    left_counts: dict[Any, int] = {}
    right_counts: dict[Any, int] = {}
    for value in left_keys: left_counts[value] = left_counts.get(value, 0) + 1
    for value in right_keys: right_counts[value] = right_counts.get(value, 0) + 1
    left_unique, right_unique = len(left_keys) == len(set(left_keys)), len(right_keys) == len(set(right_keys))
    if not left_unique and not right_unique:
        raise ToolExecutionError("many_to_many_join", "This many-to-many relationship could multiply records unsafely.")
    expected = sum(count * right_counts.get(value, 0) for value, count in left_counts.items())
    if expected > MAX_JOIN_OUTPUT_ROWS:
        raise ToolExecutionError("join_output_too_large", "The join would exceed the configured output safety limit.")
    left_columns = [_get_column_from_result(dataset, ref) for ref in args.left_columns]
    right_columns = [_get_column(right_sheet, ref) for ref in args.right_columns]
    if len(set(args.left_columns)) != len(args.left_columns) or len(set(args.right_columns)) != len(args.right_columns):
        raise ToolExecutionError("invalid_join_columns", "Join output columns must be unique.")
    if args.left_key_column_ref not in args.left_columns:
        raise ToolExecutionError("invalid_join_columns", "Join output columns must include the selected prior-result key.")
    left_data = dataset.frame[args.left_columns].copy().rename(columns={ref: f"left:{ref}" for ref in args.left_columns})
    right_refs = [column.column_id for column in right_columns]
    right_data = right_frame[[column.policy_key for column in right_columns]].copy()
    right_data.columns = [f"right:{args.right_file_id}:{ref}" for ref in right_refs]
    lk, rk = "__privy_left_join_key__", "__privy_right_join_key__"
    left_data[lk] = [normalize(value) if value is not None and not pd.isna(value) else None for value in left_values]
    right_data[rk] = [normalize(value) if value is not None and not pd.isna(value) else None for value in right_values]
    left_data, right_data = left_data[left_data[lk].notna()], right_data[right_data[rk].notna()]
    validate = "one_to_one" if left_unique and right_unique else "one_to_many" if left_unique else "many_to_one"
    try:
        joined = left_data.merge(right_data, how="inner", left_on=lk, right_on=rk, validate=validate, sort=False)
    except (pd.errors.MergeError, ValueError) as exc:
        raise ToolExecutionError("unsafe_join", "The observed join cardinality failed validation.") from exc
    if len(joined) != expected or len(joined) > max(1, len(left_data), len(right_data)):
        raise ToolExecutionError("join_amplification", "The join would produce excessive duplicate amplification.")
    right_aliases = [f"right:{args.right_file_id}:{ref}" for ref in right_refs]
    columns = {f"left:{ref}": dataset.columns[ref] for ref in args.left_columns}
    columns.update({alias: column for alias, column in zip(right_aliases, right_columns)})
    frame = pd.DataFrame({column: joined[column].reset_index(drop=True) for column in [*(f"left:{ref}" for ref in args.left_columns), *right_aliases]})
    memory = int(frame.memory_usage(index=True, deep=True).sum())
    if context.join_result_bytes + memory > MAX_JOIN_MEMORY_BYTES:
        raise ToolExecutionError("join_memory_limit", "The joined result exceeds the request memory limit.")
    sources = [*dataset.sources, _source_identity(context, args.right_file_id, args.right_worksheet_id)]
    result_ref = _new_result_ref()
    context.join_results[result_ref] = JoinedToolResult(result_ref, context.user_id, context.chat_id, frame, columns, sources, time.monotonic(), memory, [*dataset.transformations, "join_tables"], dataset.exact, list(dataset.warnings))
    context.join_result_bytes += memory
    rows = []
    for row_index, (_, row) in enumerate(joined.head(MAX_TOOL_ROWS).iterrows(), start=1):
        rows.append({"row_index": row_index, "values": {alias: _safe_value(columns[alias], row[alias]) for alias in columns}})
    return {
        "exact": dataset.exact, "verification_status": "exact" if dataset.exact else "not_verified", "validation_checks": ["joined_result_ownership_checked", "source_revisions_checked", "privacy_policies_checked", "join_keys_compatible", "cardinality_validated", "expected_size_checked", "duplicate_amplification_checked"],
        "join_type": "inner", "cardinality": validate, "matched_row_count": len(joined), "expected_matched_row_count": expected,
        "returned_row_count": len(rows), "truncated": len(joined) > len(rows), "rows": rows, "result_ref": result_ref,
        "source_identities": sources, "prior_transformations": dataset.transformations, "join_sources": [{"file_id": item["file_id"], "worksheet_id": item["table_id"]} for item in sources],
        "join_keys": [{"result_ref": args.left_result_ref, "column_ref": args.left_key_column_ref}, {"file_id": args.right_file_id, "worksheet_id": args.right_worksheet_id, "column_id": right_key.column_id}],
    }


def _get_column_from_result(dataset: JoinedToolResult, column_ref: str) -> ToolColumn:
    column = dataset.columns.get(column_ref)
    if column is None:
        raise ToolExecutionError("column_not_found", "A selected prior-result column is unavailable or excluded.")
    return column


def _aggregate_joined(context: ToolContext, raw_args: BaseModel) -> dict[str, Any]:
    args = raw_args
    assert isinstance(args, JoinedAggregateArgs)
    dataset = _validated_join_result(context, args.result_ref)
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
        "exact": dataset.exact,
        "source_identities": dataset.sources,
        "prior_transformations": dataset.transformations,
        **aggregate,
    }


def tool_capabilities() -> list[dict[str, Any]]:
    """Provider-neutral descriptions of the authoritative backend registry."""
    return [definition.capability() for definition in _TOOL_REGISTRY.values()]


def _resolve_plan_value(value: Any, results: dict[str, dict[str, Any]], dependencies: set[str]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            reference = value["$ref"]
            if not isinstance(reference, str) or reference.count(".") != 1:
                raise ToolExecutionError("invalid_plan_reference", "Plan references must name one dependency output field.")
            step_id, path = reference.split(".", 1)
            if step_id not in dependencies or step_id not in results:
                raise ToolExecutionError("invalid_plan_reference", "A plan references an undeclared or unavailable dependency.")
            current: Any = results[step_id]
            for part in path.split("."):
                if not isinstance(current, dict) or part not in current:
                    raise ToolExecutionError("invalid_plan_reference", "A plan references an unavailable output field.")
                current = current[part]
            return current
        return {key: _resolve_plan_value(item, results, dependencies) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_plan_value(item, results, dependencies) for item in value]
    return value


def _plan_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Keep only bounded result metadata needed for provider interpretation and references."""
    keys = _plan_result_summary_keys()
    summary = {key: result[key] for key in keys if key in result and key not in {"rows", "groups", "columns"}}
    if isinstance(result.get("rows"), list):
        summary["rows"] = result["rows"][:10]
    if isinstance(result.get("groups"), list):
        summary["groups"] = result["groups"][:MAX_TOOL_GROUPS]
    if isinstance(result.get("columns"), list):
        summary["columns"] = result["columns"][:20]
        summary["profile_summary_truncated"] = len(result["columns"]) > 20
    return summary


def _plan_result_summary_keys() -> tuple[str, ...]:
    return (
        "operation", "exact", "verification_status", "row_count", "matched_row_count",
        "returned_row_count", "value", "aggregation", "group_count", "result_ref",
        "source_identities", "provenance", "partial_result", "limitations", "column_ref",
        "truncated", "period_range", "period_count", "returned_group_count", "rank_method",
        "sort_direction", "sort_metric", "duplicate_row_count", "column_count", "row_count",
        "operation", "valid_count", "null_count", "invalid_count", "division_by_zero_count",
        "warnings",
        "columns", "groups", "rows",
    )


def _execute_analysis_plan(context: ToolContext, raw_args: BaseModel) -> dict[str, Any]:
    args = raw_args
    assert isinstance(args, AnalysisPlanArgs)
    steps = args.steps
    ids = [step.step_id for step in steps]
    if len(set(ids)) != len(ids):
        raise ToolExecutionError("invalid_plan", "Plan step identifiers must be unique.")
    definitions = {step_id: _TOOL_REGISTRY.get(step.capability) for step_id, step in zip(ids, steps)}
    for index, step in enumerate(steps):
        earlier = set(ids[:index])
        if len(set(step.depends_on)) != len(step.depends_on) or not set(step.depends_on) <= earlier:
            raise ToolExecutionError("invalid_plan", "Plan dependencies must refer to earlier steps and cannot be circular.")
        if definitions[step.step_id] is None or step.capability == "execute_analysis_plan":
            raise ToolExecutionError("unknown_capability", "The plan includes an unavailable analytical capability.")
        refs: list[str] = []
        for value in step.arguments.values():
            # Validate references before execution; values are later resolved against declared outputs.
            def collect(item: Any) -> None:
                if isinstance(item, dict):
                    if set(item) == {"$ref"} and isinstance(item["$ref"], str):
                        reference = item["$ref"]
                        if reference.count(".") != 1 or any(not part for part in reference.split(".")):
                            raise ToolExecutionError("invalid_plan_reference", "Plan references must name one dependency output field.")
                        parent_id, field_name = reference.split(".", 1)
                        parent_step = next((candidate for candidate in steps if candidate.step_id == parent_id), None)
                        parent_definition = _TOOL_REGISTRY.get(parent_step.capability) if parent_step else None
                        if (parent_definition is None or field_name not in _plan_result_summary_keys()
                                or field_name not in parent_definition.result_schema.get("properties", {})):
                            raise ToolExecutionError("invalid_plan_reference", "A plan reference names an unavailable or non-shareable output field.")
                        refs.append(parent_id)
                    else:
                        for nested in item.values(): collect(nested)
                elif isinstance(item, list):
                    for nested in item: collect(nested)
            collect(value)
            if any(reference not in step.depends_on for reference in refs):
                raise ToolExecutionError("invalid_plan_reference", "Every plan reference must be a declared dependency.")
        if not refs:
            try:
                definitions[step.step_id].argument_model.model_validate(step.arguments)
            except ValidationError as exc:
                raise ToolExecutionError("invalid_plan", "A plan step does not match its registered capability contract.") from exc
    depth: dict[str, int] = {}
    for step in steps:
        depth[step.step_id] = 1 + max((depth[parent] for parent in step.depends_on), default=0)
        if depth[step.step_id] > MAX_ANALYSIS_PLAN_DEPTH:
            raise ToolExecutionError("plan_depth_limit", "The plan exceeds the configured dependency depth.")

    started = time.monotonic()
    outputs: dict[str, dict[str, Any]] = {}
    summaries: list[dict[str, Any]] = []
    failed = False
    awaiting_confirmation = False
    for step in steps:
        if awaiting_confirmation:
            summaries.append({"step_id": step.step_id, "status": "paused_for_confirmation"})
            continue
        if any(parent not in outputs for parent in step.depends_on):
            summaries.append({"step_id": step.step_id, "status": "skipped_dependency_failed"})
            failed = True
            continue
        if time.monotonic() - started > MAX_ANALYSIS_PLAN_SECONDS:
            summaries.append({"step_id": step.step_id, "status": "skipped_time_limit"})
            failed = True
            continue
        try:
            resolved = _resolve_plan_value(step.arguments, outputs, set(step.depends_on))
            definition = definitions[step.step_id]
            assert definition is not None
            definition.argument_model.model_validate(resolved)
            result = execute_tool(context, step.capability, resolved)
            outputs[step.step_id] = result
            if step.capability == "discover_joins" and context.pending_join_confirmations:
                summaries.append({"step_id": step.step_id, "status": "awaiting_confirmation", "result": _plan_result_summary(result)})
                awaiting_confirmation = True
            else:
                summaries.append({"step_id": step.step_id, "status": "completed", "result": _plan_result_summary(result)})
        except (ToolExecutionError, ValidationError) as exc:
            safe_code = exc.code if isinstance(exc, ToolExecutionError) else "invalid_arguments"
            safe_message = exc.safe_message if isinstance(exc, ToolExecutionError) else "The plan step arguments did not match the capability contract."
            summaries.append({"step_id": step.step_id, "status": "failed", "error": {"code": safe_code, "message": safe_message}})
            failed = True
            # Independent later steps may still run; dependent steps are skipped above.
        except Exception:
            summaries.append({"step_id": step.step_id, "status": "failed", "error": {
                "code": "operation_failed", "message": "The workbook operation could not be completed safely.",
            }})
            failed = True
    sources = []
    for result in outputs.values():
        for identity in result.get("source_identities", []):
            if identity not in sources:
                sources.append(identity)
    return {
        "plan_version": 1,
        "exact": all(item.get("exact") is True for result in outputs.values() for item in [result]) and not failed and not awaiting_confirmation,
        "awaiting_confirmation": awaiting_confirmation,
        "partial_result": failed and bool(outputs),
        "limitations": (["User approval is required before the pending join can run."] if awaiting_confirmation else []) +
                      (["Some plan steps failed or were skipped; only completed step results are included."] if failed else []),
        "completed_step_count": len(outputs),
        "source_identities": sources,
        "steps": summaries,
    }


_REGISTERED_OPERATION_NAMES = [
    "discover_joins", "join_tables", "aggregate_joined_data", "get_workbook_schema",
    "get_column_profile", "profile_dataset", "filter_rows", "aggregate_data", "group_and_aggregate",
    "compare_groups", "trend_analysis", "calculate_columns",
]


def tool_definitions() -> list[ToolDefinition]:
    common = frozenset({"read_workbook"})
    return [
        ToolDefinition(
            name="execute_analysis_plan",
            description="Validate and run a bounded dependency-aware plan using only registered analytical capabilities. Use {$ref:'step_id.output_field'} for declared dependencies. The backend executes steps in order and returns bounded summaries, never complete intermediate datasets.",
            parameters={
                "type": "object",
                "properties": {
                    "objective": {"type": "string", "maxLength": 512},
                    "steps": {"type": "array", "minItems": 1, "maxItems": MAX_ANALYSIS_PLAN_STEPS, "items": {
                        "type": "object",
                        "properties": {
                            "step_id": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_-]*$"},
                            "capability": {"type": "string", "enum": _REGISTERED_OPERATION_NAMES},
                            "arguments": {"type": "object"},
                            "depends_on": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_ANALYSIS_PLAN_STEPS},
                        },
                        "required": ["step_id", "capability", "arguments"],
                        "additionalProperties": False,
                    }},
                },
                "required": ["objective", "steps"],
                "additionalProperties": False,
            },
            required_permissions=frozenset({"read_workbook"}),
            argument_model=AnalysisPlanArgs,
            handler=_execute_analysis_plan,
            result_schema={"type": "object", "properties": {"plan_version": {"type": "integer"}, "exact": {"type": "boolean"}, "awaiting_confirmation": {"type": "boolean"}, "partial_result": {"type": "boolean"}, "completed_step_count": {"type": "integer"}, "steps": {"type": "array"}}, "required": ["plan_version", "exact", "awaiting_confirmation", "partial_result", "completed_step_count", "steps"]},
            max_result_bytes=MAX_TOOL_RESULT_BYTES,
        ),
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
                    "left_result_ref": {"type": "string", "description": "A complete joined result from an earlier step in this request."},
                    "left_key_column_ref": {"type": "string", "description": "A Share-permitted key column reference from that joined result."},
                    "right_file_id": {"type": "string"},
                    "right_worksheet_id": {"type": "string"},
                    "right_key_column_id": {"type": "string"},
                    "left_columns": {"type": "array", "minItems": 1, "maxItems": MAX_FILTER_COLUMNS, "items": {"type": "string"}},
                    "right_columns": {"type": "array", "minItems": 1, "maxItems": MAX_FILTER_COLUMNS, "items": {"type": "string"}},
                },
                "required": ["right_file_id", "right_worksheet_id", "right_key_column_id", "left_columns", "right_columns"],
                "additionalProperties": False,
            },
            required_permissions=common,
            argument_model=JoinArgs,
            handler=_join_tables,
            result_schema={"type": "object", "properties": {"exact": {"type": "boolean"}, "cardinality": {"type": "string"}, "matched_row_count": {"type": "integer"}, "returned_row_count": {"type": "integer"}, "result_ref": {"type": "string"}, "rows": {"type": "array"}}, "required": ["exact", "cardinality", "matched_row_count", "returned_row_count", "result_ref", "rows"]},
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
            name="profile_dataset",
            description="Profile an authorized worksheet without returning columns: types, missing and duplicate counts, distinctness, decimal descriptive statistics, safe grouping hints, exact duplicate-row count, and IQR outlier counts.",
            parameters={"type": "object", "properties": {
                "file_id": {"type": "string"}, "worksheet_id": {"type": "string"},
                "include_outliers": {"type": "boolean"},
            }, "required": ["file_id", "worksheet_id"], "additionalProperties": False},
            required_permissions=common,
            argument_model=DatasetProfileArgs,
            handler=_profile_dataset,
            result_schema={"type": "object", "properties": {"workbook_id": {"type": "string"}, "worksheet_id": {"type": "string"}, "row_count": {"type": "integer"}, "column_count": {"type": "integer"}, "duplicate_row_count": {"type": "integer"}, "exact": {"type": "boolean"}, "columns": {"type": "array"}}, "required": ["row_count", "column_count", "duplicate_row_count", "exact", "columns"]},
            max_result_bytes=MAX_TOOL_RESULT_BYTES,
        ),
        ToolDefinition(
            name="filter_rows",
            description="Filter a complete authorized worksheet or current request-scoped result using bounded AND/OR conditions, optionally sort by up to three authorized columns, and return a safe sample plus a local result reference for downstream plan steps.",
            parameters={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "worksheet_id": {"type": "string"},
                    "result_ref": {"type": "string"},
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
                    "sort_by": {"type": "array", "maxItems": 3, "items": {"type": "object", "properties": {
                        "column_id": {"type": "string"}, "direction": {"type": "string", "enum": ["ascending", "descending"]},
                    }, "required": ["column_id"], "additionalProperties": False}},
                },
                "required": ["conditions", "columns"],
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
            description="Group a complete authorized worksheet or current request-scoped result by one permitted categorical column; apply one primary and up to four additional exact aggregates, rank by a selected metric, and return bounded top or bottom groups.",
            parameters={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "worksheet_id": {"type": "string"},
                    "result_ref": {"type": "string"},
                    "group_column_id": {"type": "string"},
                    "group_column_ids": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string"}},
                    "aggregation": _aggregation_schema(),
                    "metric_column_id": {"type": ["string", "null"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_TOOL_GROUPS},
                    "additional_aggregations": {"type": "array", "maxItems": 4, "items": {"type": "object", "properties": {
                        "alias": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]*$"},
                        "aggregation": _aggregation_schema(), "metric_column_id": {"type": ["string", "null"]},
                    }, "required": ["alias", "aggregation"], "additionalProperties": False}},
                    "sort_direction": {"type": "string", "enum": ["none", "ascending", "descending"]},
                    "sort_metric": {"type": "string"},
                    "rank_method": {"type": "string", "enum": ["first", "dense", "min"]},
                    "include_ties": {"type": "boolean"},
                },
                "required": ["aggregation"],
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
            description="Compare two to five explicitly selected groups using an exact aggregate. Optional change reports absolute difference and percentage change, treating the first group as baseline and returning null percent change for a zero or missing baseline.",
            parameters={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "worksheet_id": {"type": "string"},
                    "result_ref": {"type": "string"},
                    "group_column_id": {"type": "string"},
                    "group_values": {"type": "array", "minItems": 2, "maxItems": 5, "items": {"type": "string"}},
                    "aggregation": _aggregation_schema(),
                    "metric_column_id": {"type": ["string", "null"]},
                    "include_change": {"type": "boolean"},
                },
                "required": ["group_column_id", "group_values", "aggregation"],
                "additionalProperties": False,
            },
            required_permissions=common,
            argument_model=CompareGroupsArgs,
            handler=_compare_groups,
            result_schema=_RESULT_SCHEMAS["compare_groups"],
            max_result_bytes=MAX_TOOL_RESULT_BYTES,
        ),
        ToolDefinition(
            name="trend_analysis",
            description="Aggregate complete authorized records by valid day, Monday-based week, month, quarter, or year periods; output is chronological and reports missing or invalid dates and truncation.",
            parameters={"type": "object", "properties": {
                "file_id": {"type": "string"}, "worksheet_id": {"type": "string"},
                "date_column_id": {"type": "string"},
                "interval": {"type": "string", "enum": ["day", "week", "month", "quarter", "year"]},
                "aggregation": _aggregation_schema(), "metric_column_id": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_TOOL_GROUPS},
            }, "required": ["file_id", "worksheet_id", "date_column_id", "interval", "aggregation"], "additionalProperties": False},
            required_permissions=common,
            argument_model=TrendArgs,
            handler=_trend_analysis,
            result_schema={"type": "object", "properties": {"period_count": {"type": "integer"}, "period_range": {"type": ["object", "null"]}, "missing_date_count": {"type": "integer"}, "invalid_date_count": {"type": "integer"}, "exact": {"type": "boolean"}, "groups": {"type": "array"}}, "required": ["period_count", "missing_date_count", "invalid_date_count", "exact", "groups"]},
            max_result_bytes=MAX_TOOL_RESULT_BYTES,
        ),
        ToolDefinition(
            name="calculate_columns",
            description="Create a bounded request-scoped derived column using only add, subtract, multiply, divide, percent_change, or days_between. The complete derived values remain local and can feed aggregate_joined_data through the returned result_ref and column_ref.",
            parameters={"type": "object", "properties": {
                "file_id": {"type": "string"}, "worksheet_id": {"type": "string"},
                "result_ref": {"type": "string"},
                "operation": {"type": "string", "enum": ["add", "subtract", "multiply", "divide", "percent_change", "days_between"]},
                "left_column_id": {"type": "string"}, "right_column_id": {"type": "string"},
                "alias": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]*$"},
            }, "required": ["operation", "left_column_id", "right_column_id", "alias"], "additionalProperties": False},
            required_permissions=common,
            argument_model=DerivedColumnArgs,
            handler=_calculate_columns,
            result_schema={"type": "object", "properties": {"result_ref": {"type": "string"}, "column_ref": {"type": "string"}, "operation": {"type": "string"}, "row_count": {"type": "integer"}, "valid_count": {"type": "integer"}, "null_count": {"type": "integer"}, "invalid_count": {"type": "integer"}, "division_by_zero_count": {"type": "integer"}, "rounded_count": {"type": "integer"}, "numeric_precision": {"type": "string"}, "exact": {"type": "boolean"}}, "required": ["result_ref", "column_ref", "operation", "row_count", "exact"]},
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
    return [value for value in values.tolist() if not _is_null_cell(value)]


def _is_null_cell(value: Any) -> bool:
    return value is None or pd.isna(value) or (isinstance(value, str) and not value.strip())


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
        return _is_null_cell(actual)
    if operator == "not_null":
        return not _is_null_cell(actual)
    if _is_null_cell(actual):
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


def _decimal_percentile(values: list[Decimal], percentile: int) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    position = Decimal(percentile) * Decimal(len(ordered) - 1) / Decimal(100)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - Decimal(lower)
    with localcontext() as decimal_context:
        decimal_context.prec = max(38, max(len(value.as_tuple().digits) for value in ordered) + 16)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _profile_dataset(context: ToolContext, raw_args: BaseModel) -> dict[str, Any]:
    args = raw_args
    assert isinstance(args, DatasetProfileArgs)
    sheet = _get_sheet(context, args.file_id, args.worksheet_id)
    frame = _ensure_frame(context, args.file_id, sheet)
    columns = []
    for column in sheet.columns:
        values = _series(sheet, column, frame)
        non_null = _non_null(values)
        kind = _column_kind(non_null)
        frequencies: dict[str, int] = {}
        for value in non_null:
            key = str(value)
            frequencies[key] = frequencies.get(key, 0) + 1
        duplicate_values = sum(count - 1 for count in frequencies.values() if count > 1)
        column_result: dict[str, Any] = {
            "column_id": column.column_id,
            "name": _schema_name(column.name, column.position),
            "data_type": column.data_type or kind,
            "inferred_type": kind,
            "total_count": len(values),
            "missing_count": len(values) - len(non_null),
            "missing_percentage": round((len(values) - len(non_null)) * 100 / max(1, len(values)), 3),
            "distinct_count": len(frequencies),
            "is_unique": bool(non_null) and len(frequencies) == len(non_null),
            "duplicate_value_count": duplicate_values,
            "duplicate_distinct_value_count": sum(1 for count in frequencies.values() if count > 1),
            "constant": bool(non_null) and len(frequencies) == 1,
            "near_constant": bool(non_null) and max(frequencies.values(), default=0) / len(non_null) >= 0.98,
            "useful_for_grouping": not column.protected and kind == "categorical" and 1 < len(frequencies) <= MAX_TOOL_GROUPS,
        }
        if not column.protected and kind == "numeric" and non_null:
            numeric = [_as_decimal(value) for value in non_null]
            min_exponent = min(value.as_tuple().exponent for value in numeric)
            max_adjusted = max(value.adjusted() for value in numeric)
            statistics_precision = max(64, max_adjusted - min_exponent + len(str(len(numeric))) + 12)
            with localcontext() as decimal_context:
                decimal_context.prec = statistics_precision
                total = sum(numeric, Decimal(0))
                mean = total / Decimal(len(numeric))
                median = _decimal_percentile(numeric, 50)
                column_result.update({
                    "minimum": format(min(numeric), "f"),
                    "maximum": format(max(numeric), "f"),
                    "mean": format(mean, "f"),
                    "median": format(median, "f") if median is not None else None,
                    "percentile_25": format(_decimal_percentile(numeric, 25), "f"),
                    "percentile_75": format(_decimal_percentile(numeric, 75), "f"),
                    "stddev_sample": format((sum(((value - mean) ** 2 for value in numeric), Decimal(0)) / Decimal(max(1, len(numeric) - 1))).sqrt(), "f") if len(numeric) > 1 else None,
                    "statistics_precision": f"decimal_{statistics_precision}_significant_digits",
                })
                if args.include_outliers:
                    q1, q3 = _decimal_percentile(numeric, 25), _decimal_percentile(numeric, 75)
                    assert q1 is not None and q3 is not None
                    spread = q3 - q1
                    low, high = q1 - Decimal("1.5") * spread, q3 + Decimal("1.5") * spread
                    column_result.update({
                        "outlier_method": "IQR_1.5",
                        "percentile_method": "linear_interpolation",
                        "outlier_count": sum(1 for value in numeric if value < low or value > high),
                        "outlier_lower_fence": format(low, "f"),
                        "outlier_upper_fence": format(high, "f"),
                    })
        elif not column.protected and kind == "date" and non_null:
            parsed_dates = [_date_comparison_value(value) for value in non_null]
            column_result.update({
                "minimum": min(parsed_dates).isoformat(),
                "maximum": max(parsed_dates).isoformat(),
            })
        columns.append(column_result)
    permitted_keys = [column.policy_key for column in sheet.columns]
    duplicate_row_count = int(frame[permitted_keys].duplicated(keep="first").sum()) if permitted_keys else 0
    return {
        "workbook_id": args.file_id,
        "worksheet_id": sheet.worksheet_id,
        "row_count": len(frame),
        "column_count": len(sheet.columns),
        "exact": True,
        "duplicate_row_count": duplicate_row_count,
        "duplicate_row_method": "exact_duplicate_across_available_columns",
        "columns": columns,
    }


def _store_derived_result(
    context: ToolContext,
    frame: pd.DataFrame,
    columns: dict[str, ToolColumn],
    source_identities: list[dict[str, Any]],
    transformations: list[str],
    exact: bool = True,
    warnings: list[str] | None = None,
) -> str:
    memory = int(frame.memory_usage(index=True, deep=True).sum())
    if context.join_result_bytes + memory > MAX_JOIN_MEMORY_BYTES:
        raise ToolExecutionError("join_memory_limit", "The derived result exceeds the request memory limit.")
    result_ref = _new_result_ref()
    context.join_results[result_ref] = JoinedToolResult(
        result_ref, context.user_id, context.chat_id, frame, columns, source_identities,
        time.monotonic(), memory, transformations, exact, warnings or [],
    )
    context.join_result_bytes += memory
    return result_ref


def _calculate_columns(context: ToolContext, raw_args: BaseModel) -> dict[str, Any]:
    args = raw_args
    assert isinstance(args, DerivedColumnArgs)
    if bool(args.result_ref) == bool(args.file_id or args.worksheet_id):
        raise ToolExecutionError("invalid_sources", "Choose either an attached worksheet or one current result reference.")
    dataset = _validated_join_result(context, args.result_ref) if args.result_ref else None
    if dataset:
        columns = [ToolColumn(ref, ref, column.name, column.data_type, column.action, column.sensitive,
                              column.share_confirmed, column.position)
                   for ref, column in dataset.columns.items()]
        sheet = ToolWorksheet("derived_result", "Derived result", 0, len(dataset.frame), len(columns),
                              "visible", False, columns, dataset.frame)
        source_frame = dataset.frame
        source_identities = dataset.sources
        prior_transformations = dataset.transformations
    else:
        if not args.file_id or not args.worksheet_id:
            raise ToolExecutionError("invalid_sources", "A derived calculation needs an authorized worksheet or current result reference.")
        sheet = _get_sheet(context, args.file_id, args.worksheet_id)
        source_frame = _ensure_frame(context, args.file_id, sheet)
        source_identities = [_source_identity(context, args.file_id, args.worksheet_id)]
        prior_transformations = []
    left = _get_column(sheet, args.left_column_id)
    right = _get_column(sheet, args.right_column_id)
    if any(column.action != "share" or column.protected or column.sensitive for column in (left, right)):
        raise ToolExecutionError("column_not_shareable", "Derived calculations require non-sensitive Share-permitted columns.")
    operations = {"add", "subtract", "multiply", "divide", "percent_change", "days_between"}
    if args.operation not in operations:
        raise ToolExecutionError("invalid_calculation", "That cross-column calculation is not supported.")
    if args.operation == "days_between":
        if _column_kind(_non_null(_series(sheet, left, source_frame))) != "date" or _column_kind(_non_null(_series(sheet, right, source_frame))) != "date":
            raise ToolExecutionError("invalid_calculation", "Date differences require two consistently parseable date columns.")
    else:
        for column in (left, right):
            kind = _column_kind(_non_null(_series(sheet, column, source_frame)))
            if kind not in {"numeric", "empty"}:
                raise ToolExecutionError("invalid_calculation", "Arithmetic requires consistently numeric columns.")
    calculated: list[Decimal | None] = []
    null_count = invalid_count = division_by_zero_count = 0
    rounded_count = 0
    for left_value, right_value in zip(_series(sheet, left, source_frame), _series(sheet, right, source_frame)):
        if _is_null_cell(left_value) or _is_null_cell(right_value):
            calculated.append(None)
            null_count += 1
            continue
        try:
            with localcontext() as decimal_context:
                decimal_context.prec = 128
                if args.operation == "days_between":
                    result_value = Decimal((_date_comparison_value(left_value) - _date_comparison_value(right_value)).days)
                else:
                    first, second = _as_decimal(left_value), _as_decimal(right_value)
                    if args.operation == "add": result_value = first + second
                    elif args.operation == "subtract": result_value = first - second
                    elif args.operation == "multiply": result_value = first * second
                    else:
                        if second == 0:
                            calculated.append(None)
                            division_by_zero_count += 1
                            continue
                        result_value = (first - second) / second * Decimal(100) if args.operation == "percent_change" else first / second
                        exact_check = (first - second) * Decimal(100) if args.operation == "percent_change" else first
                        if result_value * second != exact_check:
                            rounded_count += 1
                if len(result_value.as_tuple().digits) > 128 or not result_value.is_finite():
                    raise InvalidOperation
                calculated.append(result_value)
        except (InvalidOperation, ToolExecutionError, OverflowError):
            calculated.append(None)
            invalid_count += 1
    alias_ref = f"derived:{args.alias}"
    derived_column = ToolColumn(alias_ref, alias_ref, args.alias, "DATE_DIFFERENCE" if args.operation == "days_between" else "DECIMAL",
                                "share", False, True, max(left.position, right.position) + 1)
    retained = [column for column in sheet.columns if column.action == "share" and not column.protected and not column.sensitive]
    result_frame = pd.DataFrame({column.column_id: _series(sheet, column, source_frame).reset_index(drop=True) for column in retained})
    result_frame[alias_ref] = calculated
    result_columns = {column.column_id: column for column in retained}
    result_columns[alias_ref] = derived_column
    calculation_warnings = ( ["Null or invalid operand rows were omitted from downstream numeric aggregates."] if null_count or invalid_count else [] ) + \
                            ( ["Division by zero produced null derived values."] if division_by_zero_count else [] ) + \
                            ( ["Non-terminating decimal results were rounded to 128 significant digits."] if rounded_count else [] ) + list(dataset.warnings if dataset else [])
    result_ref = _store_derived_result(context, result_frame, result_columns,
                                       source_identities, [*prior_transformations, "calculate_columns"],
                                       exact=(dataset.exact if dataset else True) and rounded_count == 0,
                                       warnings=calculation_warnings)
    sample = [{"row_index": index + 1, "value": _safe_value(derived_column, value)}
              for index, value in enumerate(calculated[:MAX_TOOL_ROWS])]
    return {
        "result_ref": result_ref,
        "workbook_id": args.file_id,
        "worksheet_id": args.worksheet_id,
        "column_ref": alias_ref,
        "operation": args.operation,
        "row_count": len(source_frame),
        "valid_count": len(calculated) - null_count - invalid_count - division_by_zero_count,
        "null_count": null_count,
        "invalid_count": invalid_count,
        "division_by_zero_count": division_by_zero_count,
        "rounded_count": rounded_count,
        "numeric_precision": "decimal_128_significant_digits",
        "exact": (dataset.exact if dataset else True) and rounded_count == 0,
        "truncated": len(calculated) > len(sample),
        "sample": sample,
        "warnings": calculation_warnings,
        "source_identities": source_identities,
        "prior_transformations": prior_transformations,
    }


def _filter_rows(context: ToolContext, raw_args: BaseModel) -> dict[str, Any]:
    args = raw_args
    assert isinstance(args, FilterArgs)
    if args.match not in {"all", "any"}:
        raise ToolExecutionError("invalid_filter", "The filter combination must be 'all' or 'any'.")
    if len(set(args.columns)) != len(args.columns):
        raise ToolExecutionError("invalid_filter", "Requested columns must be unique.")
    if bool(args.result_ref) == bool(args.file_id or args.worksheet_id):
        raise ToolExecutionError("invalid_sources", "Choose either an attached worksheet or one current result reference.")
    dataset = _validated_join_result(context, args.result_ref) if args.result_ref else None
    if dataset:
        columns_by_id = dataset.columns
        frame = dataset.frame
        if any(column_id not in columns_by_id for column_id in args.columns):
            raise ToolExecutionError("column_not_found", "A requested output column is unavailable or excluded.")
        result_columns = [columns_by_id[column_id] for column_id in args.columns]
        all_columns = list(columns_by_id.values())
        source_identities = dataset.sources
        prior_transformations = dataset.transformations
        source_row_count = len(frame)
        get_values = lambda column: frame[column.column_id]
    else:
        if not args.file_id or not args.worksheet_id:
            raise ToolExecutionError("invalid_sources", "A filter needs an authorized worksheet or current result reference.")
        sheet = _get_sheet(context, args.file_id, args.worksheet_id)
        frame = _ensure_frame(context, args.file_id, sheet)
        result_columns = [_get_column(sheet, column_id) for column_id in args.columns]
        all_columns = list(sheet.columns)
        source_identities = [_source_identity(context, args.file_id, args.worksheet_id)]
        prior_transformations = []
        source_row_count = len(frame)
        get_values = lambda column: _series(sheet, column, frame)
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
        column = columns_by_id.get(condition.column_id) if dataset else _get_column(sheet, condition.column_id)
        if column is None:
            raise ToolExecutionError("column_not_found", "A filter column is unavailable or excluded.")
        values = _non_null(get_values(column))
        conditions.append((column, condition, _column_kind(values)))
    matches = []
    for row_index, row in frame.iterrows():
        row_matches = [
            _check_match(
                condition.operator,
                row.get(column.column_id if dataset else column.policy_key),
                condition.value,
                kind,
            )
            for column, condition, kind in conditions
        ]
        if all(row_matches) if args.match == "all" else any(row_matches):
            matches.append((row_index, row))
    if args.sort_by:
        if len({item.column_id for item in args.sort_by}) != len(args.sort_by):
            raise ToolExecutionError("invalid_sort", "Sort columns must be unique.")
        sort_frame = pd.DataFrame(index=[index for index, _ in matches])
        directions = []
        sort_names = []
        for position, specification in enumerate(args.sort_by):
            if specification.direction not in {"ascending", "descending"}:
                raise ToolExecutionError("invalid_sort", "Sort direction must be ascending or descending.")
            column = columns_by_id.get(specification.column_id) if dataset else _get_column(sheet, specification.column_id)
            if column is None:
                raise ToolExecutionError("column_not_found", "A sort column is unavailable or excluded.")
            values = _non_null(get_values(column))
            kind = _column_kind(values)
            if kind == "mixed":
                raise ToolExecutionError("invalid_sort", "Mixed-type columns cannot be sorted reliably.")
            def normalize_sort(value: Any) -> Any:
                if _is_null_cell(value): return None
                if kind == "numeric": return _as_decimal(value)
                if kind == "date": return _date_comparison_value(value)
                return str(value).casefold()
            sort_name = f"sort_{position}"
            sort_frame[sort_name] = [normalize_sort(frame.at[index, column.column_id if dataset else column.policy_key]) for index in sort_frame.index]
            sort_names.append(sort_name)
            directions.append(specification.direction == "ascending")
        ordered_indices = sort_frame.sort_values(sort_names, ascending=directions, na_position="last", kind="mergesort").index.tolist()
        by_index = {index: row for index, row in matches}
        matches = [(index, by_index[index]) for index in ordered_indices]
    rows = []
    for index, row in matches[:args.limit]:
        rows.append({
            "row_index": int(index) + 1,
            "values": {
                column.column_id: _safe_value(column, row.get(column.column_id if dataset else column.policy_key))
                for column in result_columns
            },
        })
    if dataset:
        retained_frame = frame.loc[[index for index, _ in matches], list(frame.columns)].copy()
        result_columns_by_id = columns_by_id
    else:
        retained_frame = pd.DataFrame({column.column_id: frame.loc[[index for index, _ in matches], column.policy_key].reset_index(drop=True)
                                       for column in all_columns})
        result_columns_by_id = {column.column_id: column for column in all_columns}
    result_ref = _store_derived_result(context, retained_frame, result_columns_by_id,
        source_identities, [*prior_transformations, "filter_rows"],
        exact=dataset.exact if dataset else True,
        warnings=list(dataset.warnings) if dataset else [])
    return {
        "workbook_id": args.file_id,
        "worksheet_id": args.worksheet_id,
        "exact": dataset.exact if dataset else True,
        "matched_row_count": len(matches),
        "returned_row_count": len(rows),
        "truncated": len(matches) > len(rows),
        "result_ref": result_ref,
        "sort_by": [{"column_id": item.column_id, "direction": item.direction} for item in args.sort_by],
        "source_identities": source_identities if dataset else [],
        "prior_transformations": prior_transformations,
        "warnings": list(dataset.warnings) if dataset else [],
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
    additional_aggregations: list[GroupMetric] | None = None,
    sort_direction: str = "none",
    sort_metric: str = "primary",
    rank_method: str = "first",
    include_ties: bool = False,
    additional_group_columns: list[ToolColumn] | None = None,
) -> tuple[list[dict[str, Any]], int, bool]:
    if aggregation not in _AGGREGATIONS:
        raise ToolExecutionError("invalid_aggregation", "That aggregation is not supported.")
    if aggregation != "count" and metric_column is None:
        raise ToolExecutionError("missing_column", "Select a metric column for this aggregation.")
    group_columns = [group_column, *(additional_group_columns or [])]
    group_series = [_series(sheet, column, frame) for column in group_columns]
    metric_series = _series(sheet, metric_column, frame) if metric_column else None
    groups: dict[tuple[tuple[bool, str], ...], list[Any]] = {}
    for index in frame.index:
        parts = []
        for series in group_series:
            value = series.loc[index]
            is_null = _is_null_cell(value)
            parts.append((is_null, "" if is_null else str(value)))
        key = tuple(parts)
        groups.setdefault(key, []).append(index)
        if len(groups) > MAX_ANALYSIS_GROUP_CARDINALITY:
            raise ToolExecutionError("too_many_groups", "The group cardinality exceeds the configured analysis limit.")
    metric_requests = [GroupMetric(alias="primary", aggregation=aggregation, metric_column_id=metric_column.column_id if metric_column else None)]
    metric_requests.extend(additional_aggregations or [])
    if len({metric.alias for metric in metric_requests}) != len(metric_requests):
        raise ToolExecutionError("invalid_aggregation", "Aggregation aliases must be unique.")
    computed = []
    for group_key, group_indexes in groups.items():
        values = [metric_series.loc[index] for index in group_indexes] if metric_series is not None else [True] * len(group_indexes)
        non_null_values = [value for value in values if value is not None and not pd.isna(value)]
        aggregates = {"primary": _compute_aggregate(non_null_values, aggregation, metric_column)}
        for metric in metric_requests[1:]:
            if metric.aggregation not in _AGGREGATIONS:
                raise ToolExecutionError("invalid_aggregation", "That aggregation is not supported.")
            selected = _get_column(sheet, metric.metric_column_id) if metric.metric_column_id else None
            metric_series = _series(sheet, selected, frame) if selected else None
            local_values = [metric_series.loc[index] for index in group_indexes] if metric_series is not None else [True] * len(group_indexes)
            aggregates[metric.alias] = _compute_aggregate(_non_null(pd.Series(local_values)), metric.aggregation, selected)
        labels = []
        for (is_null, group_value), column in zip(group_key, group_columns):
            label = "<NULL>" if is_null else group_value
            labels.append(f"Group {len(computed) + 1}" if not is_null and (column.protected or scan_for_unmasked_pii(label)) else label)
        safe_label: Any = labels[0] if len(labels) == 1 else {column.column_id: label for column, label in zip(group_columns, labels)}
        computed.append({
            "_key": group_key,
            "_sort_value": aggregates.get(sort_metric, {}).get("value"),
            "group": safe_label,
            "row_count": len(values),
            **aggregates["primary"],
            **({"aggregate_values": {key: value for key, value in aggregates.items() if key != "primary"}} if additional_aggregations else {}),
        })
    if sort_direction not in {"none", "ascending", "descending"}:
        raise ToolExecutionError("invalid_sort", "Group sort direction must be none, ascending, or descending.")
    if sort_metric not in {"primary", *(metric.alias for metric in metric_requests[1:])}:
        raise ToolExecutionError("invalid_sort", "The requested ranking metric is not available.")
    if rank_method not in {"first", "dense", "min"}:
        raise ToolExecutionError("invalid_rank", "Rank method must be first, dense, or min.")
    if sort_direction != "none":
        def sort_key(item: dict[str, Any]) -> tuple[bool, Any, str]:
            value = item.get("_sort_value")
            if value is None:
                return True, Decimal(0), str(item["_key"])
            try:
                normalized: Any = _as_decimal(value)
            except InvalidOperation:
                normalized = str(value).casefold()
            return False, normalized, str(item["_key"])
        eligible = [item for item in computed if item.get("_sort_value") is not None]
        nulls = [item for item in computed if item.get("_sort_value") is None]
        eligible.sort(key=sort_key, reverse=(sort_direction == "descending"))
        computed = [*eligible, *nulls]
        for item in computed:
            item.pop("_sort_value", None)
    else:
        computed.sort(key=lambda item: item["_key"])
        for item in computed:
            item.pop("_sort_value", None)
    chosen = computed[:limit]
    if include_ties and sort_direction != "none" and len(computed) > limit and chosen:
        boundary = chosen[-1].get("aggregate_values", {}).get(sort_metric, {}).get("value") if sort_metric != "primary" else chosen[-1].get("value")
        for candidate in computed[limit:]:
            candidate_value = candidate.get("aggregate_values", {}).get(sort_metric, {}).get("value") if sort_metric != "primary" else candidate.get("value")
            if candidate_value != boundary or len(chosen) >= MAX_TOOL_GROUPS:
                break
            chosen.append(candidate)
    ranks: dict[Any, int] = {}
    previous_value: Any = object()
    dense_rank = 0
    for position, item in enumerate(chosen, start=1):
        if sort_direction != "none":
            value = item.get("aggregate_values", {}).get(sort_metric, {}).get("value") if sort_metric != "primary" else item.get("value")
            normalized = str(value)
            if normalized != str(previous_value):
                dense_rank += 1
                ranks[normalized] = position if rank_method == "min" else dense_rank
                previous_value = value
            item["rank"] = position if rank_method == "first" else ranks[normalized]
    truncated = len(chosen) < len(computed)
    for item in chosen:
        item.pop("_key", None)
    return chosen, len(groups), truncated


def _group_aggregate(context: ToolContext, raw_args: BaseModel) -> dict[str, Any]:
    args = raw_args
    assert isinstance(args, GroupAggregateArgs)
    if bool(args.result_ref) == bool(args.file_id or args.worksheet_id):
        raise ToolExecutionError("invalid_sources", "Choose either an attached worksheet or one current result reference.")
    dataset = _validated_join_result(context, args.result_ref) if args.result_ref else None
    if dataset:
        columns = [ToolColumn(ref, ref, column.name, column.data_type, column.action, column.sensitive,
                              column.share_confirmed, column.position)
                   for ref, column in dataset.columns.items()]
        sheet = ToolWorksheet("derived_result", "Derived result", 0, len(dataset.frame), len(columns),
                              "visible", False, columns, dataset.frame)
        frame = dataset.frame
        source_identities = dataset.sources
        prior_transformations = dataset.transformations
        workbook_id = None
        worksheet_id = None
    else:
        if not args.file_id or not args.worksheet_id:
            raise ToolExecutionError("invalid_sources", "Grouping needs an authorized worksheet or current result reference.")
        sheet = _get_sheet(context, args.file_id, args.worksheet_id)
        frame = _ensure_frame(context, args.file_id, sheet)
        source_identities = []
        prior_transformations = []
        workbook_id = args.file_id
        worksheet_id = sheet.worksheet_id
    group_column_ids = args.group_column_ids or ([args.group_column_id] if args.group_column_id else [])
    if not group_column_ids or len(set(group_column_ids)) != len(group_column_ids):
        raise ToolExecutionError("invalid_group", "Choose one to three unique grouping columns.")
    group_columns = [_get_column(sheet, column_id) for column_id in group_column_ids]
    group_column = group_columns[0]
    metric_column = _get_column(sheet, args.metric_column_id) if args.metric_column_id else None
    groups, group_count, truncated = _group_aggregate_values(
        sheet, group_column, metric_column, frame, args.aggregation, args.limit,
        args.additional_aggregations, args.sort_direction, args.sort_metric,
        args.rank_method, args.include_ties, group_columns[1:],
    )
    return {
        "workbook_id": workbook_id,
        "worksheet_id": worksheet_id,
        "aggregation": args.aggregation,
        "row_count": len(frame),
        "group_count": group_count,
        "returned_group_count": len(groups),
        "truncated": truncated,
        "sort_direction": args.sort_direction,
        "sort_metric": args.sort_metric,
        "rank_method": args.rank_method,
        "exact": dataset.exact if dataset else True,
        "source_identities": source_identities,
        "prior_transformations": prior_transformations,
        "warnings": list(dataset.warnings) if dataset else [],
        "groups": groups,
    }


def _trend_bucket(value: date | datetime, interval: str) -> date:
    day = value.date() if isinstance(value, datetime) else value
    if interval == "day": return day
    if interval == "week": return day - timedelta(days=day.weekday())
    if interval == "month": return day.replace(day=1)
    if interval == "quarter": return date(day.year, ((day.month - 1) // 3) * 3 + 1, 1)
    if interval == "year": return date(day.year, 1, 1)
    raise ToolExecutionError("invalid_interval", "Choose day, week, month, quarter, or year.")


def _trend_analysis(context: ToolContext, raw_args: BaseModel) -> dict[str, Any]:
    args = raw_args
    assert isinstance(args, TrendArgs)
    if args.interval not in {"day", "week", "month", "quarter", "year"}:
        raise ToolExecutionError("invalid_interval", "Choose day, week, month, quarter, or year.")
    if args.aggregation not in _AGGREGATIONS:
        raise ToolExecutionError("invalid_aggregation", "That aggregation is not supported.")
    sheet = _get_sheet(context, args.file_id, args.worksheet_id)
    date_column = _get_column(sheet, args.date_column_id)
    metric_column = _get_column(sheet, args.metric_column_id) if args.metric_column_id else None
    if date_column.protected or date_column.sensitive:
        raise ToolExecutionError("column_not_shareable", "Trend periods cannot be derived from a protected date column.")
    if args.aggregation != "count" and metric_column is None:
        raise ToolExecutionError("missing_column", "Select a metric column for this trend aggregation.")
    frame = _ensure_frame(context, args.file_id, sheet)
    date_values = _series(sheet, date_column, frame)
    metric_values = _series(sheet, metric_column, frame) if metric_column else None
    groups: dict[date, list[Any]] = {}
    invalid_count = missing_count = 0
    for index, raw_date in date_values.items():
        if _is_null_cell(raw_date):
            missing_count += 1
            continue
        try:
            period = _trend_bucket(_parse_iso_date(raw_date), args.interval)
        except ToolExecutionError:
            invalid_count += 1
            continue
        groups.setdefault(period, []).append(metric_values.loc[index] if metric_values is not None else True)
        if len(groups) > MAX_ANALYSIS_GROUP_CARDINALITY:
            raise ToolExecutionError("too_many_periods", "The trend exceeds the configured period limit.")
    periods = sorted(groups)
    summaries = []
    for period in periods:
        values = _non_null(pd.Series(groups[period]))
        summaries.append({"period": period.isoformat(), "row_count": len(groups[period]), **_compute_aggregate(values, args.aggregation, metric_column)})
    returned = summaries[:args.limit]
    return {
        "workbook_id": args.file_id,
        "worksheet_id": args.worksheet_id,
        "date_column_id": date_column.column_id,
        "metric_column_id": metric_column.column_id if metric_column else None,
        "interval": args.interval,
        "aggregation": args.aggregation,
        "row_count": len(frame),
        "period_count": len(summaries),
        "returned_period_count": len(returned),
        "period_range": {"start": periods[0].isoformat(), "end": periods[-1].isoformat()} if periods else None,
        "missing_date_count": missing_count,
        "invalid_date_count": invalid_count,
        "exact": True,
        "truncated": len(returned) < len(summaries),
        "groups": returned,
        "warnings": (["Rows with missing or invalid dates were excluded from the trend."] if missing_count or invalid_count else []),
    }


def _compare_groups(context: ToolContext, raw_args: BaseModel) -> dict[str, Any]:
    args = raw_args
    assert isinstance(args, CompareGroupsArgs)
    if len(set(args.group_values)) != len(args.group_values):
        raise ToolExecutionError("invalid_groups", "Comparison groups must be unique.")
    if any(len(value) > 256 for value in args.group_values):
        raise ToolExecutionError("invalid_groups", "Comparison group values must be 256 characters or fewer.")
    if bool(args.result_ref) == bool(args.file_id or args.worksheet_id):
        raise ToolExecutionError("invalid_sources", "Choose either an attached worksheet or one current result reference.")
    dataset = _validated_join_result(context, args.result_ref) if args.result_ref else None
    if dataset:
        columns = [ToolColumn(ref, ref, column.name, column.data_type, column.action, column.sensitive,
                              column.share_confirmed, column.position)
                   for ref, column in dataset.columns.items()]
        sheet = ToolWorksheet("derived_result", "Derived result", 0, len(dataset.frame), len(columns),
                              "visible", False, columns, dataset.frame)
        frame = dataset.frame
        source_identities = dataset.sources
        prior_transformations = dataset.transformations
    else:
        if not args.file_id or not args.worksheet_id:
            raise ToolExecutionError("invalid_sources", "A comparison needs an authorized worksheet or current result reference.")
        sheet = _get_sheet(context, args.file_id, args.worksheet_id)
        frame = _ensure_frame(context, args.file_id, sheet)
        source_identities = []
        prior_transformations = []
    group_column = _get_column(sheet, args.group_column_id)
    metric_column = _get_column(sheet, args.metric_column_id) if args.metric_column_id else None
    group_series = _series(sheet, group_column, frame)
    metric_series = _series(sheet, metric_column, frame) if metric_column else None
    comparisons = []
    for group_index, expected in enumerate(args.group_values, start=1):
        indices = [
            index for index, value in group_series.items()
            if not _is_null_cell(value) and str(value).casefold() == expected.casefold()
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
    comparison_result = {
        "workbook_id": args.file_id,
        "worksheet_id": sheet.worksheet_id,
        "aggregation": args.aggregation,
        "row_count": len(frame),
        "exact": dataset.exact if dataset else True,
        "source_identities": source_identities,
        "prior_transformations": prior_transformations,
        "warnings": list(dataset.warnings) if dataset else [],
        "comparisons": comparisons,
    }
    if args.include_change:
        if len(comparisons) != 2:
            raise ToolExecutionError("invalid_comparison", "Percentage change requires exactly two groups; the first is the baseline.")
        baseline, current = comparisons
        baseline_value, current_value = baseline.get("value"), current.get("value")
        absolute_difference = None
        percentage_change = None
        percentage_rounded = False
        baseline_is_zero = baseline_value is None
        if baseline_value is not None and current_value is not None:
            try:
                first, second = _as_decimal(baseline_value), _as_decimal(current_value)
                baseline_is_zero = first == 0
                absolute_difference = format(second - first, "f")
                if first != 0:
                    with localcontext() as decimal_context:
                        decimal_context.prec = 48
                        percentage = (second - first) / first * Decimal(100)
                        percentage_change = format(percentage, "f")
                        exact_fraction = Fraction(second - first) * 100 / Fraction(first)
                        denominator = exact_fraction.denominator
                        while denominator % 2 == 0:
                            denominator //= 2
                        while denominator % 5 == 0:
                            denominator //= 5
                        percentage_rounded = denominator != 1
            except InvalidOperation as exc:
                raise ToolExecutionError("invalid_comparison", "Percentage change requires numeric aggregate results.") from exc
        comparison_result.update({
            "comparison_definition": {"baseline_group": baseline.get("group"), "current_group": current.get("group"), "baseline_zero_behavior": "percentage_change_null"},
            "absolute_difference": absolute_difference,
            "percentage_change": percentage_change,
            "exact": comparison_result["exact"] and not percentage_rounded,
            "comparison_precision": "decimal_48_significant_digits",
            "warnings": ((["Percentage change is undefined because the baseline is zero or missing."] if baseline_is_zero else []) +
                         (["Percentage change was rounded to 48 significant digits."] if percentage_rounded else []) +
                         (list(dataset.warnings) if dataset else [])),
        })
    return comparison_result


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
        right_id = arguments.get("right_file_id")
        if not isinstance(right_id, str):
            raise ToolExecutionError("invalid_arguments", "Join file identifiers must be strings.")
        _refresh_authorized_files(context, None)
        if right_id not in context.files:
            raise ToolExecutionError("file_not_found", "One or more requested files are not attached to this chat.")
        if not arguments.get("left_result_ref"):
            left_id = arguments.get("left_file_id")
            if not isinstance(left_id, str) or left_id not in context.files:
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
    execution_limit = MAX_ANALYSIS_PLAN_SECONDS if name == "execute_analysis_plan" else MAX_TOOL_EXECUTION_SECONDS
    if time.monotonic() - started > execution_limit:
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
