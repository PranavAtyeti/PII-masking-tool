"""Upload, preview, list, and remove spreadsheet files for a chat."""

import io
import json
import logging
import time

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from .. import mapping_store as store
from ..auth import get_current_app_user
from ..detection import classify_dataframe_columns
from ..gemini_files import delete_gemini_file_cache, get_or_upload_gemini_file
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
from ..security_scan import scan_for_unmasked_pii

router = APIRouter(prefix="/api/upload", tags=["upload"])
logger = logging.getLogger(__name__)
PREPARE_GEMINI_ON_UPLOAD = __import__("os").getenv("PRIVY_PREPARE_GEMINI_ON_UPLOAD", "true").strip().lower() not in {"0", "false", "no", "off"}
SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xls"}
GUEST_MAX_FILES = int(__import__("os").getenv("PRIVY_GUEST_MAX_FILES", "3"))
GUEST_MAX_FILE_SIZE_MB = int(__import__("os").getenv("PRIVY_GUEST_MAX_FILE_SIZE_MB", "10"))


def _get_chat_or_404(chat_id: str, user_id: str) -> dict:
    chat = store.get_chat(chat_id, user_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


def _read_dataframe(filename: str, raw_bytes: bytes) -> pd.DataFrame:
    lower = filename.lower()
    if lower.endswith(".csv"):
        return pd.read_csv(io.BytesIO(raw_bytes))
    if lower.endswith(".xlsx") or lower.endswith(".xls"):
        return pd.read_excel(io.BytesIO(raw_bytes))
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

    raw_bytes = await file.read()
    try:
        df = _read_dataframe(file.filename or "upload.csv", raw_bytes)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail="Couldn't read the uploaded file. Please check the file format and try again.") from e

    col_types = classify_dataframe_columns(df)
    columns = [
        ColumnInfo(
            name=str(col),
            type=col_types.get(col),
            enabled=bool(col_types.get(col)),
        )
        for col in df.columns
    ]
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
    file_id: str | None = Form(None),
    user: dict = Depends(get_current_app_user),
):
    """Mask one file and add/replace it as an attachment on the chat."""
    started_at = time.perf_counter()
    _get_chat_or_404(chat_id, user["auth0_sub"])

    if file_id and not store.get_chat_file(chat_id, file_id):
        raise HTTPException(status_code=404, detail="File not found")

    if user.get("role") == "guest" and not file_id and store.count_user_files(user["auth0_sub"]) >= GUEST_MAX_FILES:
        raise HTTPException(status_code=403, detail=f"Guest sessions can attach up to {GUEST_MAX_FILES} files. Sign in for more.")

    raw_bytes = await file.read()
    try:
        df = _read_dataframe(file.filename or "upload.csv", raw_bytes)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail="Couldn't read the uploaded file. Please check the file format and try again.") from e
    parsed_at = time.perf_counter()

    disabled = {c.strip() for c in disabled_columns.split(",") if c.strip()}
    col_types = classify_dataframe_columns(df)

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

    enabled_columns = set(df.columns) - disabled
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

    residual_pii = scan_for_unmasked_pii(masked_csv)
    if residual_pii:
        labels = ", ".join(f"{item["type"]} ({item["count"]})" for item in residual_pii)
        raise HTTPException(
            status_code=422,
            detail=(
                "Masking validation found possible unmasked sensitive data in the masked file "
                f"({labels}). Nothing was saved."
            ),
        )
    validated_at = time.perf_counter()
    columns = [
        ColumnInfo(name=str(col), type=col_types.get(col), enabled=col not in disabled)
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
    )
    persisted_at = time.perf_counter()

    if PREPARE_GEMINI_ON_UPLOAD:
        gemini_api_key = _active_gemini_api_key()
        if gemini_api_key:
            _prepare_gemini_provider_file(
                chat_id=chat_id,
                file_id=resolved_file_id,
                filename=file.filename or "upload.csv",
                masked_csv=masked_csv,
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
    gemini_api_key = __import__("os").getenv("GEMINI_API_KEY", "").strip() or None
    delete_gemini_file_cache(file_id, gemini_api_key)

    if not store.delete_chat_file(chat_id, file_id):
        raise HTTPException(status_code=404, detail="File not found")
    log_event(logger, "file_deleted", chat_id=chat_id, file_id=file_id)
