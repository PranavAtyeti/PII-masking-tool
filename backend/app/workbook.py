"""Provider-independent spreadsheet workbook representation and serialization."""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

import pandas as pd
from fastapi import HTTPException

from .context_limits import MAX_ROWS_PER_FILE
from .privacy_policy import recommendation
from .security_scan import scan_for_unmasked_pii

MAX_CELLS_PER_FILE = int(os.getenv("PRIVY_MAX_CELLS_PER_FILE", "2000000"))
MAX_CONTEXT_SAMPLE_ROWS = int(os.getenv("PRIVY_CONTEXT_SAMPLE_ROWS", "25"))
MAX_FALLBACK_CONTEXT_COLUMNS = int(os.getenv("PRIVY_CONTEXT_FALLBACK_COLUMNS", "6"))
_COLUMN_NAMESPACE = uuid.UUID("3256d452-49a4-4f15-9b93-252f39e17ae7")


def column_identity(sheet_position: int, column_position: int, name: str) -> str:
    sheet_id = f"sheet-{sheet_position + 1}"
    return str(uuid.uuid5(_COLUMN_NAMESPACE, f"{sheet_id}:{column_position}:{name}"))


def _single_line_context_value(value: Any) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


@dataclass
class ParsedWorksheet:
    name: str
    position: int
    state: str
    original_headers: list[str]
    headers: list[str]
    dataframe: pd.DataFrame
    source_rows: list[int]
    irregular: bool


@dataclass
class ParsedWorkbook:
    file_format: str
    file_size_bytes: int
    worksheets: list[ParsedWorksheet]


def _headers(values: list[Any]) -> tuple[list[str], list[str]]:
    original = ["" if value is None else str(value) for value in values]
    width = max(1, len(original))
    if len(original) < width:
        original.extend([""] * (width - len(original)))
    names: list[str] = []
    seen: dict[str, int] = {}
    for index, value in enumerate(original, start=1):
        base = value.strip() or f"Column {index}"
        count = seen.get(base.casefold(), 0) + 1
        seen[base.casefold()] = count
        names.append(base if count == 1 else f"{base} ({count})")
    return original, names


def _check_limits(row_count: int, cell_count: int) -> None:
    if row_count > MAX_ROWS_PER_FILE:
        raise HTTPException(
            status_code=413,
            detail=f"This spreadsheet exceeds the {MAX_ROWS_PER_FILE:,}-row limit.",
        )
    if cell_count > MAX_CELLS_PER_FILE:
        raise HTTPException(
            status_code=413,
            detail=f"This spreadsheet exceeds the {MAX_CELLS_PER_FILE:,}-cell limit.",
        )


def _csv_workbook(raw_bytes: bytes) -> list[ParsedWorksheet]:
    try:
        text = raw_bytes.decode("utf-8-sig", errors="strict")
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="CSV must use UTF-8 or UTF-8 with BOM encoding.") from exc
    except csv.Error as exc:
        raise HTTPException(status_code=400, detail="The CSV file is malformed.") from exc
    if not rows:
        raise HTTPException(status_code=400, detail="The spreadsheet must have a header row.")

    original_headers, headers = _headers(rows[0])
    max_width = max([len(headers), *(len(row) for row in rows[1:])])
    if max_width > len(headers):
        for index in range(len(headers) + 1, max_width + 1):
            original_headers.append("")
            headers.append(f"Column {index}")
    data_rows = rows[1:]
    cells = sum(len(row) for row in rows)
    _check_limits(len(data_rows), cells)
    normalized_rows = [
        [value if index < len(row) else None for index, value in enumerate(row + [None] * (len(headers) - len(row)))]
        for row in data_rows
    ]
    frame = pd.DataFrame(normalized_rows, columns=headers, dtype=object)
    return [
        ParsedWorksheet(
            name="CSV",
            position=0,
            state="visible",
            original_headers=original_headers,
            headers=headers,
            dataframe=frame,
            source_rows=list(range(2, len(data_rows) + 2)),
            irregular=any(len(row) != len(rows[0]) for row in data_rows),
        )
    ]


def _excel_value(cell: Any) -> Any:
    value = cell.value
    if value is None:
        return None
    if cell.data_type == "f":
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number_format = str(getattr(cell, "number_format", "") or "")
        if re.fullmatch(r"0{2,}", number_format):
            return str(int(value)).zfill(len(number_format))
    return value


def _xlsx_workbook(raw_bytes: bytes) -> list[ParsedWorksheet]:
    from openpyxl import load_workbook

    workbook = None
    worksheets: list[ParsedWorksheet] = []
    total_rows = total_cells = 0
    try:
        workbook = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=False)
        declared_rows = sum(max(0, (sheet.max_row or 0) - 1) for sheet in workbook.worksheets)
        declared_cells = sum((sheet.max_row or 0) * (sheet.max_column or 0) for sheet in workbook.worksheets)
        _check_limits(declared_rows, declared_cells)
        for position, sheet in enumerate(workbook.worksheets):
            iterator = sheet.iter_rows()
            first = next(iterator, ())
            first_values = [_excel_value(cell) for cell in first]
            populated_header = any(value is not None and str(value).strip() for value in first_values)
            if not populated_header and sheet.max_row == 1 and not any(first_values):
                worksheets.append(
                    ParsedWorksheet(sheet.title, position, sheet.sheet_state, [], [], pd.DataFrame(), [], False)
                )
                continue
            original_headers, headers = _headers(first_values)
            data_rows: list[list[Any]] = []
            source_rows: list[int] = []
            irregular = False
            for source_row, row in enumerate(iterator, start=2):
                values = [_excel_value(cell) for cell in row]
                if len(values) > len(headers):
                    for column_position in range(len(headers) + 1, len(values) + 1):
                        original_headers.append("")
                        headers.append(f"Column {column_position}")
                    data_rows = [
                        previous + [None] * (len(headers) - len(previous))
                        for previous in data_rows
                    ]
                if len(values) != len(headers):
                    irregular = True
                values = values[:len(headers)] + [None] * max(0, len(headers) - len(values))
                data_rows.append(values)
                source_rows.append(source_row)
                total_rows += 1
                total_cells += len(values)
                _check_limits(total_rows, total_cells)
            total_cells += len(first_values)
            _check_limits(total_rows, total_cells)
            frame = pd.DataFrame(data_rows, columns=headers, dtype=object)
            worksheets.append(
                ParsedWorksheet(
                    sheet.title,
                    position,
                    sheet.sheet_state,
                    original_headers,
                    headers,
                    frame,
                    source_rows,
                    irregular,
                )
            )
        if not worksheets:
            raise HTTPException(status_code=400, detail="The Excel workbook has no worksheets.")
        if not any(sheet.headers for sheet in worksheets):
            raise HTTPException(status_code=400, detail="The Excel workbook contains no header rows.")
        return worksheets
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="The Excel workbook could not be safely parsed.") from exc
    finally:
        if workbook is not None:
            workbook.close()


def parse_workbook(filename: str, raw_bytes: bytes) -> ParsedWorkbook:
    if not raw_bytes or not raw_bytes.strip():
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    extension = PurePath(filename).suffix.lower()
    if extension == ".csv":
        sheets = _csv_workbook(raw_bytes)
        file_format = "csv"
    elif extension == ".xlsx":
        sheets = _xlsx_workbook(raw_bytes)
        file_format = "xlsx"
    elif extension == ".xls":
        try:
            excel = pd.ExcelFile(io.BytesIO(raw_bytes))
            sheets = []
            total_rows = total_cells = 0
            for position, sheet_name in enumerate(excel.sheet_names):
                source_sheet = excel.book.sheet_by_index(position)
                total_rows += max(0, source_sheet.nrows - 1)
                total_cells += source_sheet.nrows * source_sheet.ncols
                _check_limits(total_rows, total_cells)
                raw_sheet = pd.read_excel(excel, sheet_name=sheet_name, header=None, dtype=object)
                if raw_sheet.empty:
                    sheets.append(
                        ParsedWorksheet(sheet_name, position, "visible", [], [], pd.DataFrame(), [], False)
                    )
                    continue
                original_headers, headers = _headers(raw_sheet.iloc[0].tolist())
                data = raw_sheet.iloc[1:].copy()
                data.columns = headers
                sheets.append(
                    ParsedWorksheet(
                        sheet_name,
                        position,
                        "visible",
                        original_headers,
                        headers,
                        data.astype(object),
                        list(range(2, len(data) + 2)),
                        False,
                    )
                )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail="The XLS workbook is malformed or could not be parsed.") from exc
        if not sheets:
            raise HTTPException(status_code=400, detail="The Excel workbook has no worksheets.")
        if not any(sheet.headers for sheet in sheets):
            raise HTTPException(status_code=400, detail="The Excel workbook contains no header rows.")
        file_format = "xls"
    else:
        raise HTTPException(status_code=400, detail="Unsupported file type. Use one of: .csv, .xls, .xlsx")
    return ParsedWorkbook(file_format, len(raw_bytes), sheets)


def worksheet_summaries(workbook: ParsedWorkbook) -> list[dict[str, Any]]:
    return [
        {
            "worksheet_id": f"sheet-{sheet.position + 1}",
            "name": sheet.name,
            "position": sheet.position,
            "row_count": len(sheet.dataframe),
            "column_count": len(sheet.headers),
            "visibility": sheet.state,
            "has_header": bool(sheet.headers),
            "is_empty": len(sheet.dataframe) == 0,
            "is_irregular": sheet.irregular,
        }
        for sheet in workbook.worksheets
    ]


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if pd.isna(value):
        return None
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def build_workbook_document(
    parsed: ParsedWorkbook,
    file_id: str,
    policies: dict[str, dict],
    column_types: dict[str, dict[str, str | None]],
    masked_frames: dict[str, pd.DataFrame],
) -> dict[str, Any]:
    sheet_documents = []
    total_rows = total_columns = 0
    for sheet in parsed.worksheets:
        sheet_id = f"sheet-{sheet.position + 1}"
        frame = masked_frames[sheet_id]
        columns = []
        for position, name in enumerate(sheet.headers):
            column_id = column_identity(sheet.position, position, name)
            policy_key = name if len(parsed.worksheets) == 1 else f"{sheet_id}::{name}"
            choice = policies[policy_key]
            null_count = int(frame[name].isna().sum()) if name in frame else len(frame)
            columns.append(
                {
                    "column_id": column_id,
                    "policy_key": policy_key,
                    "original_name": sheet.original_headers[position] if position < len(sheet.original_headers) else "",
                    "display_name": name,
                    "position": position,
                    "data_type": column_types[sheet_id].get(name),
                    "null_count": null_count,
                    "privacy": choice,
                    "masking_status": choice["action"],
                }
            )
        row_records = []
        for local_index, values in enumerate(frame.itertuples(index=False, name=None)):
            row_records.append(
                {
                    "source_row": sheet.source_rows[local_index] if local_index < len(sheet.source_rows) else local_index + 2,
                    "values": [_json_value(value) for value in values],
                }
            )
        total_rows += len(frame)
        total_columns += len(sheet.headers)
        sheet_documents.append(
            {
                "worksheet_id": sheet_id,
                "original_name": sheet.name,
                "position": sheet.position,
                "visibility": sheet.state,
                "row_count": len(frame),
                "column_count": len(sheet.headers),
                "header_row": 1,
                "has_header": bool(sheet.headers),
                "is_empty": len(frame) == 0,
                "is_irregular": sheet.irregular,
                "columns": columns,
                "rows": row_records,
            }
        )
    return {
        "schema_version": 1,
        "file_id": file_id,
        "file_format": parsed.file_format,
        "file_size_bytes": parsed.file_size_bytes,
        "worksheet_count": len(sheet_documents),
        "row_count": total_rows,
        "column_count": total_columns,
        "processing_status": "complete",
        "worksheets": sheet_documents,
    }


def serialize_workbook(workbook: dict[str, Any]) -> str:
    return json.dumps(workbook, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def deserialize_workbook(value: str) -> dict[str, Any]:
    try:
        workbook = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Stored workbook metadata is invalid.") from exc
    if (
        not isinstance(workbook, dict)
        or workbook.get("schema_version") != 1
        or not isinstance(workbook.get("worksheets"), list)
    ):
        raise HTTPException(status_code=422, detail="Stored workbook metadata is unsupported.")
    seen_column_ids: set[str] = set()
    for sheet in workbook["worksheets"]:
        if (
            not isinstance(sheet, dict)
            or not isinstance(sheet.get("columns"), list)
            or not isinstance(sheet.get("rows"), list)
            or not isinstance(sheet.get("position"), int)
        ):
            raise HTTPException(status_code=422, detail="Stored workbook metadata is invalid.")
        column_positions: set[int] = set()
        for column in sheet["columns"]:
            if (
                not isinstance(column, dict)
                or not isinstance(column.get("column_id"), str)
                or not isinstance(column.get("policy_key"), str)
                or not isinstance(column.get("display_name"), str)
                or not isinstance(column.get("position"), int)
                or column["position"] in column_positions
                or column["column_id"] in seen_column_ids
            ):
                raise HTTPException(status_code=422, detail="Stored workbook columns are invalid.")
            seen_column_ids.add(column["column_id"])
            column_positions.add(column["position"])
        if column_positions != set(range(len(sheet["columns"]))):
            raise HTTPException(status_code=422, detail="Stored workbook column order is invalid.")
        for row in sheet["rows"]:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("values"), list)
                or len(row["values"]) != len(sheet["columns"])
            ):
                raise HTTPException(status_code=422, detail="Stored workbook rows are invalid.")
    return workbook


def workbook_policy_projection(
    workbook: dict[str, Any],
    policy: dict[str, dict],
    selected_policy_keys: set[str] | None = None,
    *,
    worksheet_ids: set[str] | None = None,
    include_rows: bool = True,
) -> list[dict[str, Any]]:
    """Validate persisted policy and project only permitted workbook values."""
    stored_keys = {
        column.get("policy_key")
        for sheet in workbook["worksheets"]
        for column in sheet.get("columns", [])
    }
    if stored_keys != set(policy):
        raise HTTPException(status_code=422, detail="Stored workbook and privacy policy do not match.")
    if selected_policy_keys is not None and not selected_policy_keys.issubset(stored_keys):
        raise HTTPException(status_code=422, detail="Selected workbook columns are invalid.")

    projected_sheets: list[dict[str, Any]] = []
    for sheet in workbook["worksheets"]:
        columns = []
        for stored_column in sheet.get("columns", []):
            column = dict(stored_column)
            choice = policy.get(column.get("policy_key"))
            if not isinstance(choice, dict) or choice.get("action") not in {"share", "mask", "exclude"}:
                raise HTTPException(status_code=422, detail="Stored workbook privacy policy is invalid.")
            _recommended_action, expected_sensitive, _reason = recommendation(column.get("data_type"))
            if (
                choice.get("sensitive") != expected_sensitive
                or not isinstance(choice.get("share_confirmed"), bool)
            ):
                raise HTTPException(status_code=422, detail="Stored workbook sensitivity metadata is invalid.")
            if choice["share_confirmed"] and (
                choice["action"] != "share" or not expected_sensitive
            ):
                raise HTTPException(status_code=422, detail="Stored Share confirmation is invalid.")
            if choice["action"] == "share" and expected_sensitive and not choice["share_confirmed"]:
                raise HTTPException(status_code=422, detail="Sensitive Share choice is missing confirmation.")
            column["privacy"] = choice
            columns.append(column)

        selected_columns = [
            column for column in columns
            if column["privacy"]["action"] != "exclude"
            and (
                selected_policy_keys is None
                or column["policy_key"] in selected_policy_keys
            )
        ]
        headers = [str(column["display_name"]) for column in selected_columns]
        if scan_for_unmasked_pii(",".join(headers)):
            raise HTTPException(
                status_code=422,
                detail="A column header contains sensitive data and cannot be sent to a provider.",
            )
        rows = sheet.get("rows", [])
        if sheet.get("row_count") != len(rows) or sheet.get("column_count") != len(columns):
            raise HTTPException(status_code=422, detail="Stored workbook dimensions are invalid.")
        worksheet_id = f"sheet-{sheet['position'] + 1}"
        values_by_row = []
        if include_rows and (worksheet_ids is None or worksheet_id in worksheet_ids):
            for row in rows:
                values = row.get("values") if isinstance(row, dict) else None
                if (
                    not isinstance(values, list)
                    or len(values) != len(columns)
                    or not isinstance(row.get("source_row"), int)
                ):
                    raise HTTPException(status_code=422, detail="Stored workbook rows are invalid.")
                projected_values = []
                for column in selected_columns:
                    value = values[column["position"]]
                    text = "" if value is None else str(value)
                    if column["privacy"]["action"] == "mask" and scan_for_unmasked_pii(text):
                        raise HTTPException(
                            status_code=422,
                            detail="Stored workbook failed outbound privacy validation.",
                        )
                    projected_values.append(value)
                values_by_row.append({
                    "source_row": row["source_row"],
                    "values": projected_values,
                })
        projected_sheets.append({
            "worksheet_id": worksheet_id,
            "position": sheet["position"],
            "row_count": len(rows),
            "column_count": len(columns),
            "visibility": sheet.get("visibility", "visible"),
            "is_irregular": bool(sheet.get("is_irregular")),
            "columns": selected_columns,
            "rows": values_by_row,
        })
    return projected_sheets


def workbook_provider_context(
    workbook: dict[str, Any],
    policy: dict[str, dict] | None = None,
    selected_policy_keys: set[str] | None = None,
    query: str | None = None,
    sample_rows: bool = False,
) -> str:
    """Serialize only policy-permitted columns for provider context."""
    if policy is None:
        policy = {
            column["policy_key"]: column.get("privacy", {})
            for sheet in workbook["worksheets"]
            for column in sheet.get("columns", [])
        }
    projected_sheets = workbook_policy_projection(
        workbook,
        policy,
        selected_policy_keys,
    )
    query_terms = {
        term.lower()
        for term in re.findall(r"[A-Za-z0-9]+", query or "")
        if len(term) > 2
        and term.lower() not in {
            "the", "and", "for", "with", "from", "about", "show", "give",
            "what", "which", "where", "when", "this", "that", "please",
            "could", "would", "should", "have", "does", "did", "are", "was",
            "were", "data", "file", "sheet", "row", "rows", "column", "columns",
            "summarize", "summary", "overview", "tell",
        }
    }
    sections = []
    for sheet in projected_sheets:
        permitted = sheet["columns"]
        header_matches = {
            column["policy_key"]
            for column in permitted
            if query_terms.intersection(
                term.lower()
                for term in re.findall(r"[A-Za-z0-9]+", column["display_name"])
            )
        }
        selected_keys = header_matches if query is not None else None
        if selected_policy_keys is not None:
            selected_keys = set(selected_policy_keys)
        if query is not None and not header_matches and selected_policy_keys is None:
            selected_keys = {
                column["policy_key"]
                for column in permitted[:MAX_FALLBACK_CONTEXT_COLUMNS]
            }
        selected_columns = [
            column for column in permitted
            if selected_keys is None or column["policy_key"] in selected_keys
        ]
        headers = [str(column["display_name"]) for column in selected_columns]
        if sample_rows:
            headers = [_single_line_context_value(header) for header in headers]
        if scan_for_unmasked_pii(",".join(headers)):
            raise HTTPException(
                status_code=422,
                detail="A column header contains sensitive data and cannot be sent to a provider.",
            )
        source_row_header = "__privy_source_row__"
        while source_row_header.casefold() in {header.casefold() for header in headers}:
            source_row_header += "_"
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow([source_row_header, *headers])
        rows = sheet["rows"]
        if sample_rows and query is not None:
            matching_rows = []
            for row in rows:
                values = row.get("values")
                if isinstance(values, list) and query_terms.intersection(
                    term.lower()
                    for column in selected_columns
                    for term in re.findall(
                        r"[A-Za-z0-9]+",
                        str(values[permitted.index(column)] or ""),
                    )
                ):
                    matching_rows.append(row)
            rows = matching_rows or rows[:MAX_CONTEXT_SAMPLE_ROWS]
            rows = rows[:MAX_CONTEXT_SAMPLE_ROWS]
        for row in rows:
            values = row.get("values")
            cells = [row["source_row"]]
            for column in selected_columns:
                value = values[permitted.index(column)]
                text = "" if value is None else str(value)
                cells.append(_single_line_context_value(text) if sample_rows else text)
            writer.writerow(cells)
        sections.append(
            f"WORKSHEET {sheet['position'] + 1} "
            f"({sheet['row_count']} stored rows, {sheet['column_count']} columns; "
            f"{len(rows) if sample_rows else sheet['row_count']} rows in context)\n{output.getvalue()}"
        )
    return "\n\n".join(sections)


def sample_csv_context(safe_csv: str, query: str, max_rows: int = MAX_CONTEXT_SAMPLE_ROWS) -> str:
    """Select a bounded, lexical CSV sample after the privacy projection."""
    reader = csv.DictReader(io.StringIO(safe_csv, newline=""))
    headers = reader.fieldnames or []
    terms = {
        term.lower()
        for term in re.findall(r"[A-Za-z0-9]+", query)
        if len(term) > 2
        and term.lower() not in {
            "the", "and", "for", "with", "from", "about", "show", "give",
            "what", "which", "where", "when", "this", "that", "please",
            "could", "would", "should", "have", "does", "did", "are", "was",
            "were", "data", "file", "sheet", "row", "rows", "column", "columns",
            "summarize", "summary", "overview", "tell",
        }
    }
    selected_headers = [
        header for header in headers
        if terms.intersection(
            term.lower() for term in re.findall(r"[A-Za-z0-9]+", header)
        )
    ]
    if not selected_headers:
        selected_headers = headers[:MAX_FALLBACK_CONTEXT_COLUMNS]
    fallback_rows: list[tuple[int, dict]] = []
    matching_rows: list[tuple[int, dict]] = []
    stored_row_count = 0
    for row_index, row in enumerate(reader, start=2):
        stored_row_count += 1
        if len(fallback_rows) < max_rows:
            fallback_rows.append((row_index, row))
        if len(matching_rows) < max_rows and terms.intersection(
            term.lower()
            for header in selected_headers
            for term in re.findall(r"[A-Za-z0-9]+", str(row.get(header) or ""))
        ):
            matching_rows.append((row_index, row))
    selected_rows = matching_rows or fallback_rows
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["__privy_source_row__", *selected_headers])
    for row_index, row in selected_rows:
        writer.writerow([
            row_index,
            *(
                _single_line_context_value(row.get(header, "") or "")
                for header in selected_headers
            ),
        ])
    return (
        f"CSV ({stored_row_count} stored rows; {len(selected_rows)} sampled rows in context)\n"
        f"{output.getvalue()}"
    )
