"""
schemas.py
----------
Pydantic models for request/response bodies. Kept separate from the
routers so the shapes are easy to scan in one place -- useful once the
React frontend needs to mirror these as TypeScript types.
"""

from pydantic import BaseModel, Field


class ChatOut(BaseModel):
    chat_id: str
    title: str
    created_at: float
    updated_at: float


class ChatCreateIn(BaseModel):
    title: str = "New chat"


class ChatRenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class MessageIn(BaseModel):
    question: str = Field(min_length=1)
    use_ner: bool = True
    ner_confidence: float = 0.6
    concise: bool = True


class MessageOut(BaseModel):
    role: str
    content: str
    masked_count: int = 0


class AdminConfigOut(BaseModel):
    model: str
    api_key_set: bool
    api_key_preview: str = ""  # e.g. "sk-...wxyz" -- never the full key
    common_models: list[str]


class AdminConfigIn(BaseModel):
    api_key: str | None = None  # None/omitted = leave unchanged
    model: str | None = None


class ChatFileInfo(BaseModel):
    filename: str
    row_count: int
    truncated: bool
    kept_private_count: int


class ColumnInfo(BaseModel):
    name: str
    type: str | None  # e.g. "PERSON", "EMAIL"... None means not classified as PII
    enabled: bool  # whether this column was included in masking for this upload


class UploadPreviewResult(BaseModel):
    filename: str
    row_count: int
    columns: list[ColumnInfo]


class UploadResult(BaseModel):
    chat_id: str
    filename: str
    row_count: int
    truncated: bool
    columns: list[ColumnInfo]
    kept_private_count: int
    preview_csv: str  # masked preview, first rows only -- never raw
