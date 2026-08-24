"""Upload, preview, and detach spreadsheet context for a chat."""

import io
import json

import pandas as pd
from fastapi import APIRouter, HTTPException, UploadFile, File, Form

from .. import mapping_store as store
from ..detection import classify_dataframe_columns
from ..masking import mask_dataframe, build_masked_context, find_leaked_values
from ..schemas import UploadResult, UploadPreviewResult, ColumnInfo, ChatFileInfo

router = APIRouter(prefix="/api/upload", tags=["upload"])
SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xls"}


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
async def preview_file(chat_id: str, file: UploadFile = File(...)):
    """Inspect the file in memory and return detected PII columns only."""
    if not store.get_chat(chat_id):
        raise HTTPException(status_code=404, detail="Chat not found")

    raw_bytes = await file.read()
    try:
        df = _read_dataframe(file.filename or "upload.csv", raw_bytes)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Couldn't read file: {e}")
    finally:
        del raw_bytes

    col_types = classify_dataframe_columns(df)
    columns = [
        ColumnInfo(name=str(col), type=col_types.get(col), enabled=bool(col_types.get(col)))
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
):
    if not store.get_chat(chat_id):
        raise HTTPException(status_code=404, detail="Chat not found")

    raw_bytes = await file.read()
    try:
        df = _read_dataframe(file.filename or "upload.csv", raw_bytes)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Couldn't read file: {e}")
    finally:
        del raw_bytes

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
        examples_by_col = {}
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
                f"{', '.join(leaked_cols)} -- its value is still raw in the "
                f"data. Example(s): {example_lines}. Nothing was saved. This "
                "is a masking bug, not a setting to adjust -- please report it."
            ),
        )

    masked_csv, truncated = build_masked_context(masked_df)
    columns = [
        ColumnInfo(name=col, type=col_types.get(col), enabled=col not in disabled)
        for col in df.columns
    ]

    store.set_chat_file(
        chat_id=chat_id,
        filename=file.filename or "upload.csv",
        masked_csv=masked_csv,
        columns_json=json.dumps([c.model_dump() for c in columns]),
        row_count=len(df),
        truncated=truncated,
    )

    return UploadResult(
        chat_id=chat_id,
        filename=file.filename or "upload.csv",
        row_count=len(df),
        truncated=truncated,
        columns=columns,
        kept_private_count=store.session_entry_count(chat_id),
        preview_csv=masked_csv,
    )


@router.delete("/{chat_id}", status_code=204)
def delete_upload(chat_id: str):
    """Detach the masked file context from a chat without touching mappings."""
    if not store.get_chat(chat_id):
        raise HTTPException(status_code=404, detail="Chat not found")
    store.delete_chat_file(chat_id)


@router.get("/{chat_id}", response_model=ChatFileInfo)
def get_upload_info(chat_id: str):
    if not store.get_chat(chat_id):
        raise HTTPException(status_code=404, detail="Chat not found")
    chat_file = store.get_chat_file(chat_id)
    if not chat_file:
        raise HTTPException(status_code=404, detail="No file uploaded for this chat")
    return ChatFileInfo(
        filename=chat_file["filename"],
        row_count=chat_file["row_count"],
        truncated=chat_file["truncated"],
        kept_private_count=store.session_entry_count(chat_id),
    )
