"""Upload, preview, list, and remove spreadsheet files for a chat."""

import io
import json
import logging
import time
import os
import zipfile
import uuid
from pathlib import PurePath

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from .. import mapping_store as store
from ..auth import get_current_app_user
from ..detection import classify_dataframe_columns
from ..detection import scan_free_text_structured
from ..docx_ingestion import parse_docx
from ..lifecycle import ProviderCleanupPending, delete_file as delete_lifecycle_file
from ..logging_utils import elapsed_ms, log_event
from ..masking import (
    build_masked_context,
    build_masked_preview,
    count_masked_tokens,
    find_leaked_values,
    mask_free_texts,
    mask_dataframe,
)
from .. import ner_detection
from ..schemas import ChatFileInfo, ColumnInfo, UploadPreviewResult, UploadResult
from ..privacy_policy import load_policy, policy_json, recommendation, validate_policy
from ..workbook import (
    build_workbook_document,
    column_identity,
    deserialize_workbook,
    parse_workbook,
    serialize_workbook,
    worksheet_summaries,
)

router = APIRouter(prefix="/api/upload", tags=["upload"])
logger = logging.getLogger(__name__)
SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".docx"}
MAX_FILE_SIZE_BYTES = int(os.getenv("PRIVY_MAX_FILE_SIZE_BYTES", str(10 * 1024 * 1024)))
MAX_FILES_PER_CHAT = int(os.getenv("PRIVY_MAX_FILES_PER_CHAT", "10"))
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


def _read_workbook(filename: str, raw_bytes: bytes):
    extension = os.path.splitext(filename)[1].lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Use one of: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
        )
    if extension == ".xlsx":
        _validate_xlsx_zip(raw_bytes)
    return parse_workbook(filename, raw_bytes)


def _read_docx(filename: str, content_type: str | None, raw_bytes: bytes, file_id: str) -> dict:
    if PurePath(filename).suffix.lower() != ".docx":
        raise HTTPException(status_code=400, detail="Unsupported file type. Use one of: .csv, .xls, .xlsx, .docx")
    accepted_mime_types = {
        "",
        "application/octet-stream",
        "application/zip",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    if (content_type or "").lower() not in accepted_mime_types:
        raise HTTPException(status_code=400, detail="The uploaded file type does not match a DOCX document.")
    return parse_docx(raw_bytes, file_id)


def _document_sensitive_type(document: dict) -> str | None:
    sections = document.get("sections", [])
    if not sections:
        return None
    sample_count = min(16, len(sections))
    sample_indexes = (
        [0]
        if sample_count == 1
        else [
            round(index * (len(sections) - 1) / (sample_count - 1))
            for index in range(sample_count)
        ]
    )
    samples = []
    for index in sample_indexes:
        section = sections[index]
        if section.get("kind") == "table_row":
            content = " | ".join(
                str(cell.get("content", "")) for cell in section.get("cells", [])
            )
        else:
            content = str(section.get("content", ""))
        heading_path = section.get("heading_path", [])
        heading_text = " > ".join(str(item) for item in heading_path)
        sample = f"{heading_text} {content}".strip()
        if sample:
            samples.append(sample[:1000])
    texts = samples
    detected = {
        kind
        for text in texts
        for _value, kind in scan_free_text_structured(text)
    }
    try:
        ner_results = ner_detection.analyze_texts(texts, 0.6)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Document privacy classification is unavailable. Configure the local NER model and retry.",
        ) from exc
    detected.update(
        kind
        for findings in ner_results.values()
        for _value, kind, _score in findings
    )
    return next(
        (kind for kind in ("PERSON", "EMAIL", "PHONE", "ADDRESS", "ID", "DOB", "IP") if kind in detected),
        None,
    )


def _document_columns(document: dict, file_id: str) -> tuple[list[ColumnInfo], str | None]:
    sensitive_type = _document_sensitive_type(document)
    action, sensitive, reason = recommendation(sensitive_type)
    return (
        [
            ColumnInfo(
                name="document_content",
                display_name="Document content",
                worksheet_id="document",
                column_id=f"document-{file_id}",
                type=sensitive_type,
                enabled=action == "mask",
                recommended_action=action,
                sensitive=sensitive,
                reason=reason,
                action=action,
                share_confirmed=False,
            )
        ],
        sensitive_type,
    )


def _masked_document(document: dict, action: str, chat_id: str, use_ner: bool, ner_confidence: float) -> tuple[dict, int]:
    result = dict(document)
    sections = [dict(section) for section in document["sections"]]
    if action == "mask":
        sources = []
        for section in sections:
            if "content" in section:
                sources.append(section["content"])
            for cell in section.get("cells", []):
                sources.append(cell.get("content", ""))
        mapped = mask_free_texts(
            sources,
            chat_id,
            store.load_counters(chat_id),
            ner_confidence,
            use_ner,
        )
        for section in sections:
            if "content" in section:
                section["content"] = mapped.get(section["content"], section["content"])
            if "cells" in section:
                section["cells"] = [
                    {**cell, "content": mapped.get(cell.get("content", ""), cell.get("content", ""))}
                    for cell in section["cells"]
                ]
    result["sections"] = sections
    masked_text = "\n".join(
        section.get("content", "")
        or "\n".join(cell.get("content", "") for cell in section.get("cells", []))
        for section in sections
    )
    return result, count_masked_tokens(masked_text)


def _document_policy(
    submitted: dict | None,
    sensitive_type: str | None,
) -> dict[str, dict]:
    type_map = {"document_content": sensitive_type}
    action, _sensitive, _reason = recommendation(sensitive_type)
    choices = submitted if submitted is not None else {
        "document_content": {"action": action, "share_confirmed": False}
    }
    return validate_policy(["document_content"], type_map, choices)


@router.post("/{chat_id}/preview", response_model=UploadPreviewResult)
async def preview_file(
    chat_id: str,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_app_user),
):
    """Inspect one file in memory. Nothing is persisted by preview."""
    _get_chat_or_404(chat_id, user["auth0_sub"])

    raw_bytes = await _read_upload_bytes(file)
    filename = file.filename or "upload.csv"
    if PurePath(filename).suffix.lower() == ".docx":
        preview_file_id = str(uuid.uuid4())
        document = _read_docx(filename, file.content_type, raw_bytes, preview_file_id)
        columns, _sensitive_type = _document_columns(document, preview_file_id)
        return UploadPreviewResult(
            filename=filename,
            row_count=document["section_count"],
            columns=columns,
            file_format="docx",
            section_count=document["section_count"],
            warnings=document["warnings"],
        )
    try:
        workbook = _read_workbook(filename, raw_bytes)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail="Couldn't read the uploaded file. Please check the file format and try again.") from e

    columns = []
    row_count = 0
    for sheet in workbook.worksheets:
        col_types = classify_dataframe_columns(sheet.dataframe)
        row_count += len(sheet.dataframe)
        sheet_id = f"sheet-{sheet.position + 1}"
        for position, col in enumerate(sheet.headers):
            policy_key = col if len(workbook.worksheets) == 1 else f"{sheet_id}::{col}"
            action, sensitive, reason = recommendation(col_types.get(col))
            columns.append(
                ColumnInfo(
                    name=policy_key,
                    display_name=col,
                    worksheet_name=sheet.name if len(workbook.worksheets) > 1 else None,
                    worksheet_id=sheet_id,
                    worksheet_visibility=sheet.state,
                    column_id=column_identity(sheet.position, position, col),
                    type=col_types.get(col),
                    enabled=action == "mask",
                    recommended_action=action,
                    sensitive=sensitive,
                    reason=reason,
                    action=action,
                    share_confirmed=False,
                )
            )
    return UploadPreviewResult(
        filename=file.filename or "upload.csv",
        row_count=row_count,
        columns=columns,
        worksheet_count=len(workbook.worksheets),
        worksheets=worksheet_summaries(workbook),
        file_format=workbook.file_format,
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
    filename = file.filename or "upload.csv"
    if PurePath(filename).suffix.lower() == ".docx":
        resolved_file_id = file_id or str(uuid.uuid4())
        document = _read_docx(filename, file.content_type, raw_bytes, resolved_file_id)
        columns, sensitive_type = _document_columns(document, resolved_file_id)
        if privacy_policy:
            try:
                parsed_policy = json.loads(privacy_policy)
                submitted_policy = parsed_policy.get("columns") if isinstance(parsed_policy, dict) else None
                if not isinstance(submitted_policy, dict):
                    raise ValueError("invalid policy shape")
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=422, detail="The privacy choices are invalid.") from exc
        else:
            submitted_policy = None
        policy = _document_policy(submitted_policy, sensitive_type)
        document_column = columns[0].model_copy(update={
            "enabled": policy["document_content"]["action"] == "mask",
            "action": policy["document_content"]["action"],
            "share_confirmed": policy["document_content"]["share_confirmed"],
        })
        document, masked_count = _masked_document(
            document,
            policy["document_content"]["action"],
            chat_id,
            use_ner,
            ner_confidence,
        )
        store.set_chat_file(
            chat_id=chat_id,
            filename=filename,
            masked_csv="",
            columns_json=json.dumps([document_column.model_dump()]),
            row_count=document["section_count"],
            truncated=False,
            masked_count=masked_count,
            file_id=resolved_file_id,
            privacy_policy_json=policy_json(policy),
            workbook_json=json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )
        return UploadResult(
            chat_id=chat_id,
            file_id=resolved_file_id,
            filename=filename,
            row_count=document["section_count"],
            truncated=False,
            columns=[document_column],
            masked_count=masked_count,
            preview_csv="",
            privacy_policy={"version": 1, "columns": policy},
            worksheet_count=0,
            worksheets=[],
            file_format="docx",
            section_count=document["section_count"],
            warnings=document["warnings"],
        )

    try:
        workbook = _read_workbook(filename, raw_bytes)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail="Couldn't read the uploaded file. Please check the file format and try again.") from e
    parsed_at = time.perf_counter()

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
        all_policy_keys = [
            col if len(workbook.worksheets) == 1 else f"sheet-{sheet.position + 1}::{col}"
            for sheet in workbook.worksheets
            for col in sheet.headers
        ]
        submitted_policy = {
            key: {"action": "share" if key in disabled_legacy else "mask", "share_confirmed": False}
            for key in all_policy_keys
        }

    column_types: dict[str, str | None] = {}
    sheet_types: dict[str, dict[str, str | None]] = {}
    sheet_policy_keys: dict[str, dict[str, str]] = {}
    for sheet in workbook.worksheets:
        sheet_id = f"sheet-{sheet.position + 1}"
        types = classify_dataframe_columns(sheet.dataframe)
        sheet_types[sheet_id] = types
        sheet_policy_keys[sheet_id] = {}
        for col in sheet.headers:
            key = col if len(workbook.worksheets) == 1 else f"{sheet_id}::{col}"
            sheet_policy_keys[sheet_id][col] = key
            column_types[key] = types.get(col)
    all_policy_keys = [key for keys in sheet_policy_keys.values() for key in keys.values()]
    policy = validate_policy(all_policy_keys, column_types, submitted_policy)
    counters = store.load_counters(chat_id)
    masked_frames = {}
    for sheet in workbook.worksheets:
        sheet_id = f"sheet-{sheet.position + 1}"
        names = sheet_policy_keys[sheet_id]
        sheet_policy = {column: policy[key] for column, key in names.items()}
        mask_columns = {
            column for column, choice in sheet_policy.items() if choice["action"] == "mask"
        }
        disabled = set(sheet.headers) - mask_columns
        masked_frame, _known_values = mask_dataframe(
            sheet.dataframe,
            sheet_types[sheet_id],
            session_id=chat_id,
            counters=counters,
            use_ner=use_ner,
            ner_confidence=ner_confidence,
            disabled_columns=disabled,
        )
        leaked = find_leaked_values(sheet.dataframe, masked_frame, sheet_types[sheet_id], mask_columns)
        if leaked:
            leaked_cols = sorted({col for col, _ in leaked})
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Masking failed for a structured field in column(s) "
                    f"{', '.join(leaked_cols)} -- a value is still raw in the data. "
                    "Nothing was saved. This is a masking bug, not a setting to adjust -- please report it."
                ),
            )
        masked_frames[sheet_id] = masked_frame
    masked_at = time.perf_counter()
    first_sheet = next((sheet for sheet in workbook.worksheets if sheet.headers), workbook.worksheets[0])
    first_sheet_id = f"sheet-{first_sheet.position + 1}"
    masked_df = masked_frames[first_sheet_id]
    masked_csv, truncated = build_masked_context(masked_df)
    preview_csv = build_masked_preview(masked_df, max_rows=200)
    resolved_file_id = file_id or str(uuid.uuid4())
    workbook_document = build_workbook_document(
        workbook,
        resolved_file_id,
        policy,
        sheet_types,
        masked_frames,
    )
    validated_at = time.perf_counter()
    columns = []
    for sheet in workbook.worksheets:
        sheet_id = f"sheet-{sheet.position + 1}"
        for position, col in enumerate(sheet.headers):
            key = sheet_policy_keys[sheet_id][col]
            choice = policy[key]
            columns.append(
                ColumnInfo(
                    name=key,
                    display_name=col,
                    worksheet_name=sheet.name if len(workbook.worksheets) > 1 else None,
                    worksheet_id=sheet_id,
                    worksheet_visibility=sheet.state,
                    column_id=column_identity(sheet.position, position, col),
                    type=sheet_types[sheet_id].get(col),
                    enabled=choice["action"] == "mask",
                    action=choice["action"],
                    share_confirmed=choice["share_confirmed"],
                    recommended_action=choice["recommended_action"],
                    sensitive=choice["sensitive"],
                    reason=choice["reason"],
                )
            )

    # An edit supplies file_id; a new attachment gets a fresh UUID in the store.
    masked_count = count_masked_tokens(
        "\n".join(build_masked_context(frame)[0] for frame in masked_frames.values())
    )
    store.set_chat_file(
        chat_id=chat_id,
        filename=file.filename or "upload.csv",
        masked_csv=masked_csv,
        columns_json=json.dumps([c.model_dump() for c in columns]),
        row_count=sum(len(sheet.dataframe) for sheet in workbook.worksheets),
        truncated=truncated,
        masked_count=masked_count,
        file_id=resolved_file_id,
        privacy_policy_json=policy_json(policy),
        workbook_json=serialize_workbook(workbook_document),
    )
    persisted_at = time.perf_counter()

    logger.info(
        "upload_masking_timing chat_id=%s rows=%d columns=%d parse_ms=%.1f "
        "mask_ms=%.1f validate_ms=%.1f persist_ms=%.1f total_ms=%.1f",
        chat_id,
        sum(len(sheet.dataframe) for sheet in workbook.worksheets),
        len(columns),
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
        row_count=sum(len(sheet.dataframe) for sheet in workbook.worksheets),
        truncated=truncated,
        columns=columns,
        masked_count=masked_count,
        preview_csv=preview_csv,
        privacy_policy={"version": 1, "columns": policy},
        worksheet_count=len(workbook.worksheets),
        worksheets=worksheet_summaries(workbook),
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
        try:
            stored_document = json.loads(chat_file["workbook_json"]) if chat_file.get("workbook_json") else {}
        except (ValueError, TypeError):
            stored_document = {}
        is_docx = isinstance(stored_document, dict) and stored_document.get("document_kind") == "docx"
        workbook_document = (
            deserialize_workbook(chat_file["workbook_json"])
            if chat_file.get("workbook_json") and not is_docx
            else None
        )
        files.append(
            ChatFileInfo(
                file_id=chat_file["file_id"],
                filename=chat_file["filename"],
                row_count=chat_file["row_count"],
                truncated=chat_file["truncated"],
                masked_count=chat_file["masked_count"],
                columns=columns,
                privacy_policy=load_policy(chat_file.get("privacy_policy_json"), chat_file.get("columns_json"))[0],
                worksheet_count=workbook_document.get("worksheet_count", 1) if workbook_document else 1,
                worksheets=[
                    {
                        "worksheet_id": sheet.get("worksheet_id", ""),
                        "name": sheet.get("original_name", ""),
                        "position": sheet.get("position", 0),
                        "row_count": sheet.get("row_count", 0),
                        "column_count": sheet.get("column_count", 0),
                        "visibility": sheet.get("visibility", "visible"),
                        "has_header": sheet.get("has_header", False),
                        "is_empty": sheet.get("is_empty", False),
                        "is_irregular": sheet.get("is_irregular", False),
                    }
                    for sheet in (workbook_document or {}).get("worksheets", [])
                ],
                file_format="docx" if is_docx else "spreadsheet",
                processing_status=(
                    str(stored_document.get("extraction_status", "complete"))
                    if is_docx
                    else str((workbook_document or {}).get("processing_status", "complete"))
                ),
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
    try:
        deleted = delete_lifecycle_file(chat_id, file_id)
    except ProviderCleanupPending as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="File not found")
    log_event(logger, "file_deleted", chat_id=chat_id, file_id=file_id)
