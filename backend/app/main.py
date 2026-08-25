"""
main.py
-------
FastAPI entry point. Run with:  uvicorn app.main:app --reload --port 8000

This is session 1 of the Streamlit -> FastAPI+React migration: chat CRUD
and file upload/masking. The actual question-answering endpoint (LLM call +
streaming) is session 2 -- it needs network access to test against a real
provider, which isn't available in every dev environment, so it's kept as
its own piece rather than half-built here.
"""

import os

# MUST run before anything below that reads env vars at import time --
# specifically the `from .routers import ...` line, since that pulls in
# llm.py, which reads LLM_API_KEY/LLM_BASE_URL/etc into module-level
# constants the moment it's imported. Import order matters here: this is
# the same class of bug as the very first .env issue chased in the
# Streamlit version, just at the Python-import level instead of the
# process-restart level.
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import mapping_store as store
from .routers import chats, upload, messages, admin, auth

app = FastAPI(title="Privy API", version="0.1.0")

# CORS: wide open for local dev (React dev server on a different port than
# the API). Tighten this to your actual frontend origin(s) before any real
# deployment -- "*" is fine on localhost, not fine once this is reachable
# over a network.
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "http://localhost:5173").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    store.init_db()


@app.get("/api/health")
def health():
    return {"status": "ok"}


app.include_router(chats.router)
app.include_router(upload.router)
app.include_router(messages.router)
app.include_router(admin.router)
app.include_router(auth.router)
