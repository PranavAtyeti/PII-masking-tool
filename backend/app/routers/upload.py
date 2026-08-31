"""Upload, preview, list, and remove spreadsheet files for a chat."""

import io
import json

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from .. import mapping_store as store
from ..auth import get_current_app_user
from ..detection import classify_dataframe_columns
from ..masking import build_masked_context, count_masked_tokens, find_leaked_values, mask_dataframe
from ..schemas import ChatFileInfo, ColumnInfo, UploadPreviewResult, UploadResult
from ..security_scan import scan_for_unmasked_pii

router = APIRouter(prefix="/api/upload", tags=["upload"])
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
        raise HTTPException(status_code=400, detail=f"Couldn't read file: {e}") from e

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
        raise HTTPException(status_code=400, detail=f"Couldn't read file: {e}") from e

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

    enabled_columns = set(df.columns) - disabled
    leaked = find_leaked_values(df, masked_df, col_types, enabled_columns)
    if leaked:
        leaked_cols = sorted({col for col, _ in leaked})
        examples_by_col: dict[str, list[str]] = {}
        for col, val in leaked:
            examples_by_col.setdefault(col, [])
            if val not in examples_by_col[col] and len(examples_by_col[col]) < 3:
                examples_by_col[col].append(val)
        example_lines = "; ".join(
            f"{col}: {', '.join(examples_by_col[col])}" for col in leaked_cols
        )
        raise HTTPException(
            status_code=422,
            detail=(
                f"Masking failed for a structured field in column(s) "
                f"{', '.join(leaked_cols)} -- its value is still raw in the data. "
                f"Example(s): {example_lines}. Nothing was saved. This is a masking "
                "bug, not a setting to adjust -- please report it."
            ),
        )

    masked_csv, truncated = build_masked_context(masked_df)

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

    return UploadResult(
        chat_id=chat_id,
        file_id=resolved_file_id,
        filename=file.filename or "upload.csv",
        row_count=len(df),
        truncated=truncated,
        columns=columns,
        masked_count=masked_count,
        preview_csv=masked_csv,
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
    if not store.delete_chat_file(chat_id, file_id):
        raise HTTPException(status_code=404, detail="File not found")
