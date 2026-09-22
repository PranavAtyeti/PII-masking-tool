"""FastAPI entry point for the Privy backend."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Load .env before importing modules that read configuration at import time.
load_dotenv()

from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import mapping_store as store
from .logging_utils import (
    LOG_FILE_PATH,
    log_event,
    new_request_id,
    reset_request_id,
    set_request_id,
)
from .routers import chats, upload, messages, admin, auth

logger = logging.getLogger("app.main")

RUNTIME_ENVIRONMENT = os.getenv("PRIVY_ENVIRONMENT", "development").strip().lower()
IS_PRODUCTION = RUNTIME_ENVIRONMENT in {"production", "prod"}


def _csv_env(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


ALLOWED_ORIGINS = _csv_env("ALLOWED_ORIGINS", "http://localhost:5173")
ALLOWED_HOSTS = _csv_env(
    "ALLOWED_HOSTS",
    "*" if not IS_PRODUCTION else "",
)

if "*" in ALLOWED_ORIGINS:
    raise RuntimeError(
        "ALLOWED_ORIGINS must contain explicit origins when credentials are enabled."
    )

if IS_PRODUCTION and not ALLOWED_ORIGINS:
    raise RuntimeError("Set ALLOWED_ORIGINS to the deployed frontend origin(s) in production.")

if IS_PRODUCTION and not ALLOWED_HOSTS:
    raise RuntimeError("Set ALLOWED_HOSTS to the deployed API host(s) in production.")

MAX_REQUEST_BYTES = int(os.getenv("PRIVY_MAX_REQUEST_BYTES", str(50 * 1024 * 1024)))
ENABLE_API_DOCS = os.getenv(
    "PRIVY_ENABLE_API_DOCS",
    "false" if IS_PRODUCTION else "true",
).strip().lower() not in {"0", "false", "no", "off"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.init_db()
    logger.info("Privy API started environment=%s log_file=%s", RUNTIME_ENVIRONMENT, LOG_FILE_PATH)
    yield


app = FastAPI(
    title="Privy API",
    version="0.1.0",
    docs_url="/docs" if ENABLE_API_DOCS else None,
    redoc_url="/redoc" if ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_API_DOCS else None,
    lifespan=lifespan,
)


@app.middleware("http")
async def request_id_and_size_middleware(request: Request, call_next):
    request_id = new_request_id()
    token = set_request_id(request_id)
    try:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                body_size = int(content_length)
            except ValueError:
                body_size = 0
            if body_size > MAX_REQUEST_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": "Request is too large.",
                        "request_id": request_id,
                    },
                    headers={"X-Request-ID": request_id},
                )

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        reset_request_id(token)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"

    if IS_PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    return response


if IS_PRODUCTION:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Guest-Session"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    request_id = request.headers.get("X-Request-ID") or "-"
    log_event(
        logger,
        "request_validation_failed",
        level=logging.WARNING,
        path=request.url.path,
        method=request.method,
        error_count=len(exc.errors()),
    )
    return JSONResponse(
        status_code=422,
        content={"detail": "Invalid request.", "request_id": request_id},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = request.headers.get("X-Request-ID") or "-"
    # Do not log str(exc): unexpected exceptions can contain provider responses,
    # file paths, SQL fragments, or request data.
    log_event(
        logger,
        "unhandled_exception",
        level=logging.ERROR,
        path=request.url.path,
        method=request.method,
        exception_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "An internal server error occurred.",
            "request_id": request_id,
        },
    )


@app.get("/api/health")
def health():
    return {"status": "ok"}


app.include_router(chats.router)
app.include_router(upload.router)
app.include_router(messages.router)
app.include_router(admin.router)
app.include_router(auth.router)
