"""
DL Verify — FastAPI application factory
========================================
Mounts all API routers, configures CORS, registers structured error
handlers, and exposes /health and /version utility endpoints.

Usage
-----
    uvicorn app.main:app --reload              # development
    uvicorn app.main:app --host 0.0.0.0 --port 8000  # production

Environment variables
---------------------
    DL_VERIFY_ENV      : "development" | "production"  (default: development)
    DL_VERIFY_VERSION  : semver string injected at deploy time
    ALLOWED_ORIGINS    : comma-separated CORS origins
                         (default: * in development, strict in production)
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes.verify import router as verify_router
from app.utils.logger import logger

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENV = os.getenv("DL_VERIFY_ENV", "development")
VERSION = os.getenv("DL_VERIFY_VERSION", "0.1.0")

_raw_origins = os.getenv("ALLOWED_ORIGINS", "")
if _raw_origins:
    ALLOWED_ORIGINS: list[str] = [o.strip() for o in _raw_origins.split(",") if o.strip()]
elif ENV == "development":
    ALLOWED_ORIGINS = ["*"]
else:
    # Production: lock down to explicit origins — operator must set ALLOWED_ORIGINS
    ALLOWED_ORIGINS = []

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

app = FastAPI(
    title="DL Verify",
    description=(
        "AAMVA Driver's License fraud detection API. "
        "Parses PDF417 barcodes, runs cross-validation checks, "
        "and returns a structured risk score with per-signal breakdown."
    ),
    version=VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Request timing middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    t0 = time.monotonic()
    response = await call_next(request)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    response.headers["X-Process-Time-Ms"] = str(elapsed_ms)
    return response

# ---------------------------------------------------------------------------
# Structured error handlers
# ---------------------------------------------------------------------------

@app.exception_handler(404)
async def not_found_handler(request: Request, exc) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"error": "not_found", "detail": "The requested endpoint does not exist."},
    )


@app.exception_handler(405)
async def method_not_allowed_handler(request: Request, exc) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
        content={"error": "method_not_allowed", "detail": "HTTP method not allowed on this endpoint."},
    )


@app.exception_handler(413)
async def payload_too_large_handler(request: Request, exc) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        content={"error": "payload_too_large", "detail": "Uploaded file exceeds the 10 MB per-image limit."},
    )


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc) -> JSONResponse:
    logger.error("unhandled_exception", path=str(request.url), exc=str(exc))
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "internal_error", "detail": "An unexpected error occurred. Please try again."},
    )

# ---------------------------------------------------------------------------
# Utility endpoints
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    tags=["system"],
    summary="Health check",
    response_description="Service liveness indicator",
)
async def health() -> Dict[str, Any]:
    """Returns HTTP 200 when the service is alive."""
    return {"status": "ok", "env": ENV, "version": VERSION}


@app.get(
    "/version",
    tags=["system"],
    summary="Version info",
)
async def version_info() -> Dict[str, str]:
    return {"version": VERSION, "env": ENV}

# ---------------------------------------------------------------------------
# Router mounts
# ---------------------------------------------------------------------------

app.include_router(verify_router, prefix="/api/v1")

# Final route: POST /api/v1/verify

logger.info("app_startup", version=VERSION, env=ENV, allowed_origins=ALLOWED_ORIGINS)
