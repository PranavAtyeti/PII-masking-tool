"""
schemas.py
----------
Pydantic models for request/response bodies.
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
    model_id: str | None = None
    allow_unmasked_risk: bool = False


class MessageOut(BaseModel):
    role: str
    content: str
    masked_count: int = 0
    metadata: dict = Field(default_factory=dict)


class GuestSessionOut(BaseModel):
    session_id: str
    expires_at: float
    user: dict


class AdminConfigOut(BaseModel):
    model: str
    api_key_set: bool
    api_key_preview: str = ""
    common_models: list[str]


class AdminConfigIn(BaseModel):
    api_key: str | None = None
    model: str | None = None


class ColumnInfo(BaseModel):
    name: str
    type: str | None
    enabled: bool


class ChatFileInfo(BaseModel):
    file_id: str
    filename: str
    row_count: int
    truncated: bool
    masked_count: int
    columns: list[ColumnInfo]


class UploadPreviewResult(BaseModel):
    filename: str
    row_count: int
    columns: list[ColumnInfo]


class UploadResult(BaseModel):
    chat_id: str
    file_id: str
    filename: str
    row_count: int
    truncated: bool
    columns: list[ColumnInfo]
    masked_count: int
    preview_csv: str
