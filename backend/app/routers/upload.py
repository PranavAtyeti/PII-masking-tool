"""Upload, preview, list, and remove spreadsheet files for a chat."""

import io
import json
import logging
import time
import csv
import os
import zipfile

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from .. import mapping_store as store
from ..auth import get_current_app_user
from ..detection import classify_dataframe_columns
from ..gemini_files import get_or_upload_gemini_file
from ..lifecycle import delete_file as delete_lifecycle_file
from ..llm import get_model_config
from ..logging_utils import elapsed_ms, log_event
from ..masking import (
    build_masked_context,
    build_masked_preview,
    count_masked_tokens,
    find_leaked_values,
    mask_dataframe,
)
from ..schemas import ChatFileInfo, ColumnInfo, UploadPreviewResult, UploadResult
from ..privacy_policy import build_outbound_csv, load_policy, policy_json, recommendation, validate_policy
from ..context_limits import MAX_ROWS_PER_FILE

router = APIRouter(prefix="/api/upload", tags=["upload"])
logger = logging.getLogger(__name__)
PREPARE_GEMINI_ON_UPLOAD = __import__("os").getenv("PRIVY_PREPARE_GEMINI_ON_UPLOAD", "true").strip().lower() not in {"0", "false", "no", "off"}
SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xls"}
MAX_FILE_SIZE_BYTES = int(os.getenv("PRIVY_MAX_FILE_SIZE_BYTES", str(10 * 1024 * 1024)))
MAX_FILES_PER_CHAT = int(os.getenv("PRIVY_MAX_FILES_PER_CHAT", "10"))
MAX_CELLS_PER_FILE = int(os.getenv("PRIVY_MAX_CELLS_PER_FILE", "2000000"))
MAX_XLSX_ZIP_ENTRIES = int(os.getenv("PRIVY_MAX_XLSX_ZIP_ENTRIES", "10000"))
MAX_XLSX_UNCOMPRESSED_BYTES = int(os.getenv("PRIVY_MAX_XLSX_UNCOMPRESSED_BYTES", str(100 * 1024 * 1024)))
MAX_XLSX_ENTRY_BYTES = int(os.getenv("PRIVY_MAX_XLSX_ENTRY_BYTES", str(50 * 1024 * 1024)))
MAX_XLSX_COMPRESSION_RATIO = float(os.getenv("PRIVY_MAX_XLSX_COMPRESSION_RATIO", "200"))
GUEST_MAX_FILES = int(__import__("os").getenv("PRIVY_GUEST_MAX_FILES", "3"))
GUEST_MAX_FILE_SIZE_MB = int(__import__("os").getenv("PRIVY_GUEST_MAX_FILE_SIZE_MB", "10"))


def _get_chat_or_404(chat_id: str, user_id: str) -> dict:
    chat = store.get_chat(chat_id, user_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


async def _read_upload_bytes(file: UploadFile) -> bytes:
    raw_bytes = await file.read(MAX_FILE_SIZE_BYTES + 1)
    if len(raw_bytes) > MAX_FILE_SIZE_BYTES:
        raise _limit_error(f"Each file must be no larger than {MAX_FILE_SIZE_BYTES // (1024 * 1024)} MiB.")
    return raw_bytes


def _limit_error(detail: str) -> HTTPException:
    return HTTPException(status_code=413, detail=detail)


def _check_headers(headers: list[object]) -> list[str]:
    names = [str(value).strip() if value is not None else "" for value in headers]
    if not names or not any(names):
        raise HTTPException(status_code=400, detail="The spreadsheet must have a non-empty header row.")
    if any(not name for name in names):
        raise HTTPException(status_code=400, detail="Column headers cannot be blank.")
    if len(names) != len(set(names)):
        raise HTTPException(status_code=400, detail="Duplicate column headers are not supported. Rename duplicate columns and try again.")
    return names


def _check_counts(rows: int, cells: int) -> None:
    if rows > MAX_ROWS_PER_FILE:
        raise _limit_error(f"This spreadsheet exceeds the {MAX_ROWS_PER_FILE:,}-row limit.")
    if cells > MAX_CELLS_PER_FILE:
        raise _limit_error(f"This spreadsheet exceeds the {MAX_CELLS_PER_FILE:,}-cell limit.")


def _validate_csv_bytes(raw_bytes: bytes) -> None:
    if not raw_bytes or not raw_bytes.strip():
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    try:
        text = raw_bytes.decode("utf-8-sig", errors="strict")
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        headers = _check_headers(next(reader, []))
        rows = 0
        cells = len(headers)
        for row in reader:
            if not row or not any(value.strip() for value in row):
                continue
            rows += 1
            cells += sum(1 for value in row if value.strip())
            _check_counts(rows, cells)
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="CSV must use UTF-8 or UTF-8 with BOM encoding.") from exc
    except csv.Error as exc:
        raise HTTPException(status_code=400, detail="The CSV file is malformed.") from exc


def _validate_xlsx_zip(raw_bytes: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_XLSX_ZIP_ENTRIES:
                raise _limit_error(f"The Excel archive exceeds the {MAX_XLSX_ZIP_ENTRIES:,}-entry limit.")
            total = 0
            names = {entry.filename for entry in entries}
            for entry in entries:
                normalized_name = entry.filename.replace("\\", "/")
                if normalized_name.startswith("/") or ".." in normalized_name.split("/"):
                    raise HTTPException(status_code=400, detail="The XLSX archive contains a suspicious entry path.")
                if entry.flag_bits & 0x1:
                    raise HTTPException(status_code=400, detail="Encrypted XLSX archive entries are not supported.")
                if entry.file_size > MAX_XLSX_ENTRY_BYTES:
                    raise _limit_error("An Excel archive component exceeds the allowed uncompressed size.")
                total += entry.file_size
                if entry.compress_size <= 0 and entry.file_size > 0:
                    raise HTTPException(status_code=400, detail="The Excel archive has a suspicious compression ratio.")
                if entry.compress_size > 0 and entry.file_size / entry.compress_size > MAX_XLSX_COMPRESSION_RATIO:
                    raise HTTPException(status_code=400, detail="The Excel archive has a suspicious compression ratio.")
            if len(names) != len(entries):
                raise HTTPException(status_code=400, detail="The XLSX archive contains duplicate entry names.")
            if total > MAX_XLSX_UNCOMPRESSED_BYTES:
                raise _limit_error("The Excel archive exceeds the allowed total uncompressed size.")
            if "[Content_Types].xml" not in names or not any(n.startswith("xl/worksheets/") for n in names):
                raise HTTPException(status_code=400, detail="The XLSX archive is malformed.")
            bad_entry = archive.testzip()
            if bad_entry is not None:
                raise HTTPException(status_code=400, detail="The XLSX archive failed its integrity check.")
    except HTTPException:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail="The XLSX file is malformed or corrupted.") from exc


def _validate_xlsx_sheets(raw_bytes: bytes) -> None:
    from openpyxl import load_workbook

    workbook = None
    try:
        workbook = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
        if not workbook.worksheets:
            raise HTTPException(status_code=400, detail="The Excel workbook has no worksheets.")
        total_rows = total_cells = 0
        for sheet in workbook.worksheets:
            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                values = list(row)
                if index == 0:
                    _check_headers(values)
                populated = sum(value is not None and str(value).strip() != "" for value in values)
                if populated:
                    if index > 0:
                        total_rows += 1
                    total_cells += populated
                    _check_counts(total_rows, total_cells)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="The Excel workbook could not be safely parsed.") from exc
    finally:
        if workbook is not None:
            workbook.close()


def _read_dataframe(filename: str, raw_bytes: bytes) -> pd.DataFrame:
    lower = filename.lower()
    if not raw_bytes or not raw_bytes.strip():
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if lower.endswith(".csv"):
        _validate_csv_bytes(raw_bytes)
        try:
            df = pd.read_csv(io.BytesIO(raw_bytes), encoding="utf-8-sig", on_bad_lines="error")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="CSV must use UTF-8 or UTF-8 with BOM encoding.") from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail="The CSV file is malformed.") from exc
        _check_headers(list(df.columns))
        _check_counts(len(df), int(df.notna().sum().sum()))
        return df
    if lower.endswith(".xlsx"):
        _validate_xlsx_zip(raw_bytes)
        _validate_xlsx_sheets(raw_bytes)
        try:
            df = pd.read_excel(io.BytesIO(raw_bytes), sheet_name=0)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="The Excel workbook is malformed or could not be parsed.") from exc
        _check_headers(list(df.columns))
        _check_counts(len(df), int(df.notna().sum().sum()))
        return df
    if lower.endswith(".xls"):
        try:
            workbook = pd.ExcelFile(io.BytesIO(raw_bytes))
            total_rows = total_cells = 0
            first = None
            for sheet_name in workbook.sheet_names:
                raw_sheet = pd.read_excel(workbook, sheet_name=sheet_name, header=None)
                if raw_sheet.empty:
                    continue
                headers = _check_headers(raw_sheet.iloc[0].tolist())
                data = raw_sheet.iloc[1:].copy()
                data.columns = headers
                total_rows += len(data)
                total_cells += int(data.notna().sum().sum()) + len(headers)
                _check_counts(total_rows, total_cells)
                if first is None:
                    first = data
            if first is None:
                raise HTTPException(status_code=400, detail="The Excel workbook has no data or headers.")
            return first
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail="The XLS workbook is malformed or could not be parsed.") from exc
    raise HTTPException(
        status_code=400,
        detail=f"Unsupported file type. Use one of: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
    )


def _active_gemini_api_key() -> str | None:
    """Return the configured Gemini key only when Gemini is the active default model."""
    configured_model = store.get_admin_config("llm_model", "")
    try:
        if configured_model:
            provider, _base_url, api_key, _model = get_model_config(configured_model)
        else:
            provider, _base_url, api_key, _model = get_model_config(None)
    except RuntimeError:
        return None
    return api_key if provider == "gemini" else None


def _prepare_gemini_provider_file(
    *,
    chat_id: str,
    file_id: str,
    filename: str,
    masked_csv: str,
    api_key: str,
) -> None:
    started = time.perf_counter()
    log_event(
        logger,
        "gemini_upload_prepare_start",
        file_id=file_id,
        rows=masked_csv.count("\n"),
    )
    try:
        get_or_upload_gemini_file(
            file_id=file_id,
            filename=filename,
            masked_csv=masked_csv,
            api_key=api_key,
            privacy_validated=True,
        )
    except Exception as exc:
        # The local upload is already valid and persisted. Provider preparation
        # can be retried by the next Gemini question without losing the file.
        log_event(
            logger,
            "gemini_upload_prepare_error",
            level=logging.WARNING,
            file_id=file_id,
            duration_ms=elapsed_ms(started),
            error_type=type(exc).__name__,
        )
        return

    log_event(
        logger,
        "gemini_upload_prepare_complete",
        chat_id=chat_id,
        file_id=file_id,
        duration_ms=elapsed_ms(started),
    )


@router.post("/{chat_id}/preview", response_model=UploadPreviewResult)
async def preview_file(
    chat_id: str,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_app_user),
):
    """Inspect one file in memory. Nothing is persisted by preview."""
    _get_chat_or_404(chat_id, user["auth0_sub"])

    raw_bytes = await _read_upload_bytes(file)
    try:
        df = _read_dataframe(file.filename or "upload.csv", raw_bytes)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail="Couldn't read the uploaded file. Please check the file format and try again.") from e

    col_types = classify_dataframe_columns(df)
    columns = []
    for col in df.columns:
        action, sensitive, reason = recommendation(col_types.get(col))
        columns.append(ColumnInfo(name=str(col), type=col_types.get(col), enabled=action == "mask",
                                 recommended_action=action, sensitive=sensitive, reason=reason,
                                 action=action, share_confirmed=False))
    return UploadPreviewResult(
        filename=file.filename or "upload.csv",
        row_count=len(df),
        columns=columns,
    )


@router.post("/{chat_id}", response_model=UploadResult)
async def upload_file(
    chat_id: str,
    file: UploadFile = File(...),
    use_ner: bool = Form(True),
    ner_confidence: float = Form(0.6),
    disabled_columns: str = Form(""),
    privacy_policy: str | None = Form(None),
    file_id: str | None = Form(None),
    user: dict = Depends(get_current_app_user),
):
    """Mask one file and add/replace it as an attachment on the chat."""
    started_at = time.perf_counter()
    _get_chat_or_404(chat_id, user["auth0_sub"])

    if file_id and not store.get_chat_file(chat_id, file_id):
        raise HTTPException(status_code=404, detail="File not found")

    if not file_id and len(store.get_chat_files(chat_id)) >= MAX_FILES_PER_CHAT:
        raise _limit_error(f"A chat can contain at most {MAX_FILES_PER_CHAT} files.")

    if user.get("role") == "guest" and not file_id and store.count_user_files(user["auth0_sub"]) >= GUEST_MAX_FILES:
        raise HTTPException(status_code=403, detail=f"Guest sessions can attach up to {GUEST_MAX_FILES} files. Sign in for more.")

    raw_bytes = await _read_upload_bytes(file)
    try:
        df = _read_dataframe(file.filename or "upload.csv", raw_bytes)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail="Couldn't read the uploaded file. Please check the file format and try again.") from e
    parsed_at = time.perf_counter()

    col_types = classify_dataframe_columns(df)

    if privacy_policy:
        try:
            parsed_policy = json.loads(privacy_policy)
            if not isinstance(parsed_policy, dict) or not isinstance(parsed_policy.get("columns"), dict):
                raise ValueError("invalid policy shape")
            submitted_policy = parsed_policy["columns"]
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail="The privacy choices are invalid.") from exc
    else:
        disabled_legacy = {c.strip() for c in disabled_columns.split(",") if c.strip()}
        submitted_policy = {
            str(col): {"action": "share" if str(col) in disabled_legacy else "mask", "share_confirmed": False}
            for col in df.columns
        }
    policy = validate_policy([str(c) for c in df.columns], {str(k): v for k, v in col_types.items()}, submitted_policy)
    mask_columns = {name for name, choice in policy.items() if choice["action"] == "mask"}
    disabled = {str(col) for col in df.columns} - mask_columns

    counters = store.load_counters(chat_id)
    masked_df, _known_values = mask_dataframe(
        df,
        col_types,
        session_id=chat_id,
        counters=counters,
        use_ner=use_ner,
        ner_confidence=ner_confidence,
        disabled_columns=disabled,
    )
    masked_at = time.perf_counter()

    enabled_columns = mask_columns
    leaked = find_leaked_values(df, masked_df, col_types, enabled_columns)
    if leaked:
        leaked_cols = sorted({col for col, _ in leaked})
        raise HTTPException(
            status_code=422,
            detail=(
                f"Masking failed for a structured field in column(s) "
                f"{', '.join(leaked_cols)} -- a value is still raw in the data. "
                "Nothing was saved. This is a masking bug, not a setting to adjust "
                "-- please report it."
            ),
        )

    # Store the complete masked dataset for LLM analysis. The UI preview is
    # generated separately below so it never reduces the analysis dataset.
    masked_csv, truncated = build_masked_context(masked_df)
    preview_csv = build_masked_preview(masked_df, max_rows=200)

    outbound_csv = build_outbound_csv(masked_csv, policy)
    validated_at = time.perf_counter()
    columns = [
        ColumnInfo(
            name=str(col), type=col_types.get(col), enabled=policy[str(col)]["action"] == "mask",
            action=policy[str(col)]["action"], share_confirmed=policy[str(col)]["share_confirmed"],
            recommended_action=policy[str(col)]["recommended_action"], sensitive=policy[str(col)]["sensitive"],
            reason=policy[str(col)]["reason"],
        )
        for col in df.columns
    ]

    # An edit supplies file_id; a new attachment gets a fresh UUID in the store.
    masked_count = count_masked_tokens(masked_csv)

    resolved_file_id = store.set_chat_file(
        chat_id=chat_id,
        filename=file.filename or "upload.csv",
        masked_csv=masked_csv,
        columns_json=json.dumps([c.model_dump() for c in columns]),
        row_count=len(df),
        truncated=truncated,
        masked_count=masked_count,
        file_id=file_id,
        privacy_policy_json=policy_json(policy),
    )
    persisted_at = time.perf_counter()

    if PREPARE_GEMINI_ON_UPLOAD:
        gemini_api_key = _active_gemini_api_key()
        if gemini_api_key:
            _prepare_gemini_provider_file(
                chat_id=chat_id,
                file_id=resolved_file_id,
                filename=f"privy-{resolved_file_id}.csv",
                masked_csv=outbound_csv,
                api_key=gemini_api_key,
            )
        else:
            log_event(logger, "gemini_upload_prepare_skipped", reason="gemini_not_active")
    else:
        log_event(logger, "gemini_upload_prepare_skipped", reason="disabled_by_config")

    logger.info(
        "upload_masking_timing chat_id=%s rows=%d columns=%d parse_ms=%.1f "
        "mask_ms=%.1f validate_ms=%.1f persist_ms=%.1f total_ms=%.1f",
        chat_id,
        len(df),
        len(df.columns),
        (parsed_at - started_at) * 1000,
        (masked_at - parsed_at) * 1000,
        (validated_at - masked_at) * 1000,
        (persisted_at - validated_at) * 1000,
        (persisted_at - started_at) * 1000,
    )

    return UploadResult(
        chat_id=chat_id,
        file_id=resolved_file_id,
        filename=file.filename or "upload.csv",
        row_count=len(df),
        truncated=truncated,
        columns=columns,
        masked_count=masked_count,
        preview_csv=preview_csv,
        privacy_policy={"version": 1, "columns": policy},
    )


@router.get("/{chat_id}", response_model=list[ChatFileInfo])
def list_uploads(chat_id: str, user: dict = Depends(get_current_app_user)):
    _get_chat_or_404(chat_id, user["auth0_sub"])

    files: list[ChatFileInfo] = []
    for chat_file in store.get_chat_files(chat_id):
        try:
            columns_data = json.loads(chat_file["columns_json"])
            columns = [ColumnInfo(**item) for item in columns_data]
        except (ValueError, TypeError, KeyError):
            columns = []
        files.append(
            ChatFileInfo(
                file_id=chat_file["file_id"],
                filename=chat_file["filename"],
                row_count=chat_file["row_count"],
                truncated=chat_file["truncated"],
                masked_count=chat_file["masked_count"],
                columns=columns,
                privacy_policy=load_policy(chat_file.get("privacy_policy_json"), chat_file.get("columns_json"))[0],
            )
        )
    return files


@router.delete("/{chat_id}/{file_id}", status_code=204)
def delete_upload(
    chat_id: str,
    file_id: str,
    user: dict = Depends(get_current_app_user),
):
    _get_chat_or_404(chat_id, user["auth0_sub"])
    if not store.get_chat_file(chat_id, file_id):
        raise HTTPException(status_code=404, detail="File not found")

    # Best-effort provider cleanup. Gemini files expire on their own, but an
    # explicit delete keeps lifecycle ownership aligned with the Privy file.
    if not delete_lifecycle_file(chat_id, file_id):
        raise HTTPException(status_code=404, detail="File not found")
    log_event(logger, "file_deleted", chat_id=chat_id, file_id=file_id)
