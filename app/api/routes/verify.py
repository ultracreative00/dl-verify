"""
POST /verify
============
Multipart endpoint that accepts the front and back images of a US
Driver's License and runs the full Sprint-1 validation pipeline:

  1. Image quality gate   (blur / glare / crop check)
  2. Barcode detection    (PDF417 from back image)
  3. AAMVA parse          (structured document fields)
  4. Cross-validation     (6 checks from validators.py)
  5. Risk scoring         (weighted signal aggregation)

Returns a structured VerifyResponse JSON that exposes every signal
and sub-score so the calling SMB application can surface the reason
for a REVIEW / REJECT to its operator — a deliberate contrast to
Persona’s opaque output.

All heavy I/O (image bytes) is read once and passed by reference;
no temp files are written to disk.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from app.core.barcode.detector import detect_barcode
from app.core.barcode.exceptions import BarcodeNotFoundError
from app.core.barcode.parser import parse_aamva, ParsedAAMVADocument
from app.core.barcode.validators import (
    check_age_derived_fields,
    check_date_logic,
    check_dcf_entropy,
    check_expiry_window,
    check_jurisdiction_fields,
    check_syntax_conformance,
)
from app.core.image.quality import check_image_quality, ImageQualityError
from app.core.scoring.scorer import score, ScoringResult
from app.utils.logger import logger

router = APIRouter(prefix="/verify", tags=["verification"])


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------

class SignalBreakdown(BaseModel):
    """Per-check result block surfaced in the API response."""
    check: str = Field(..., description="Validator check name")
    severity: str = Field(..., description="pass | warn | fail")
    score: float = Field(..., description="Weighted contribution 0.0–1.0")
    signals: Dict[str, Any] = Field(
        default_factory=dict,
        description="All raw signals produced by this check",
    )


class ExtractedFields(BaseModel):
    """Subset of AAMVA fields returned for operator review."""
    license_number: Optional[str] = None
    family_name: Optional[str] = None
    given_name: Optional[str] = None
    middle_name: Optional[str] = None
    date_of_birth: Optional[str] = None
    expiration_date: Optional[str] = None
    issue_date: Optional[str] = None
    address_street: Optional[str] = None
    address_city: Optional[str] = None
    address_state: Optional[str] = None
    address_postal: Optional[str] = None
    sex: Optional[str] = None
    height: Optional[str] = None
    jurisdiction: Optional[str] = None
    country: Optional[str] = None
    aamva_version: Optional[int] = None


class VerifyResponse(BaseModel):
    """Top-level response from POST /verify."""
    # Decision
    recommendation: str = Field(
        ..., description="PASS | REVIEW | REJECT"
    )
    risk_score: float = Field(
        ..., description="Normalised fraud risk 0.0 (clean) to 1.0 (fraud)"
    )

    # Hard override signals
    hard_fails: List[str] = Field(
        default_factory=list,
        description="Signals that forced REJECT regardless of numeric score",
    )
    hard_warns: List[str] = Field(
        default_factory=list,
        description="Signals that lifted floor to REVIEW",
    )

    # Per-check detail
    checks: List[SignalBreakdown] = Field(
        default_factory=list,
        description="Per-validator breakdown",
    )

    # Extracted document fields
    extracted_fields: Optional[ExtractedFields] = Field(
        None, description="Key AAMVA fields extracted from the barcode"
    )

    # Pipeline metadata
    barcode_detected: bool = Field(..., description="Whether a PDF417 barcode was found")
    image_quality_passed: bool = Field(..., description="Whether the image quality gate passed")
    processing_ms: int = Field(..., description="Total server-side processing time in ms")
    pipeline_stage_reached: str = Field(
        ...,
        description="Last pipeline stage successfully completed before result was produced",
    )

    # Warnings / informational notes
    warnings: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SEX_MAP = {"1": "M", "2": "F", "9": "Not Specified"}


def _build_extracted_fields(doc: ParsedAAMVADocument) -> ExtractedFields:
    """
    Map a ParsedAAMVADocument to the public ExtractedFields response schema.

    Uses doc.normalized_fields so that date values are already in
    ISO 8601 (YYYY-MM-DD) format for display.
    """
    f = doc.normalized_fields  # plain dict[str, str] -- safe to call .get() on

    # _jurisdiction and _aamva_version are injected by some parser paths;
    # fall back gracefully when absent.
    raw_sex = f.get("DBC", "")

    return ExtractedFields(
        license_number=f.get("DAQ"),
        family_name=f.get("DCS"),
        given_name=f.get("DAC"),
        middle_name=f.get("DAD"),
        date_of_birth=f.get("DBB"),
        expiration_date=f.get("DBA"),
        issue_date=f.get("DBD"),
        address_street=f.get("DAG"),
        address_city=f.get("DAI"),
        address_state=f.get("DAJ"),
        address_postal=f.get("DAK"),
        sex=SEX_MAP.get(raw_sex, raw_sex if raw_sex else None),
        height=f.get("DAU"),
        jurisdiction=f.get("_jurisdiction") or f.get("DAJ"),
        country=f.get("DCG"),
        aamva_version=(
            int(f["_aamva_version"])
            if f.get("_aamva_version", "").isdigit()
            else None
        ),
    )


def _build_check_breakdown(
    validation_results,
    score_result: ScoringResult,
) -> List[SignalBreakdown]:
    """Zip validator results with scorer breakdown scores."""
    breakdown = []
    for vr in validation_results:
        breakdown.append(
            SignalBreakdown(
                check=vr.check,
                severity=vr.severity,
                score=score_result.score_breakdown.get(vr.check, 0.0),
                signals=vr.signals,
            )
        )
    return breakdown


def _max_upload_bytes() -> int:
    """10 MB limit per image."""
    return 10 * 1024 * 1024


async def _read_upload(upload: UploadFile, field_name: str) -> bytes:
    """Read and size-validate an upload."""
    data = await upload.read()
    if len(data) == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name}: uploaded file is empty.",
        )
    if len(data) > _max_upload_bytes():
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{field_name}: file exceeds 10 MB limit.",
        )
    return data


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.post(
    "",
    response_model=VerifyResponse,
    summary="Verify a US Driver's License",
    description=(
        "Submit front and back images of a US DL (JPEG or PNG, max 10 MB each). "
        "Returns a fraud risk score, recommendation, and full per-signal breakdown."
    ),
    status_code=status.HTTP_200_OK,
)
async def verify_document(
    front: UploadFile = File(..., description="Front image of the DL (JPEG/PNG)"),
    back: UploadFile = File(..., description="Back image of the DL — must contain the PDF417 barcode"),
) -> VerifyResponse:
    t_start = time.monotonic()
    warnings: list[str] = []
    pipeline_stage = "upload"

    # ── 1. Read uploads ───────────────────────────────────────────────
    front_bytes = await _read_upload(front, "front")
    back_bytes = await _read_upload(back, "back")

    logger.info(
        "verify_request",
        front_bytes=len(front_bytes),
        back_bytes=len(back_bytes),
        front_content_type=front.content_type,
        back_content_type=back.content_type,
    )

    # ── 2. Image quality gate ───────────────────────────────────────
    pipeline_stage = "image_quality"
    image_quality_passed = True

    try:
        front_quality = check_image_quality(front_bytes, side="front")
        back_quality = check_image_quality(back_bytes, side="back")

        if not front_quality.passed:
            image_quality_passed = False
            warnings.append(f"Front image quality issue: {front_quality.reason}")
        if not back_quality.passed:
            image_quality_passed = False
            warnings.append(f"Back image quality issue: {back_quality.reason}")

        if not image_quality_passed:
            elapsed = int((time.monotonic() - t_start) * 1000)
            return VerifyResponse(
                recommendation="REJECT",
                risk_score=1.0,
                hard_fails=["image_quality_failed"],
                hard_warns=[],
                checks=[],
                extracted_fields=None,
                barcode_detected=False,
                image_quality_passed=False,
                processing_ms=elapsed,
                pipeline_stage_reached="image_quality",
                warnings=warnings,
            )

    except ImageQualityError as exc:
        logger.warning("image_quality_error", error=str(exc))
        warnings.append(f"Image quality check could not complete: {exc}")
        image_quality_passed = False

    # ── 3. Barcode detection ───────────────────────────────────────
    pipeline_stage = "barcode_detection"

    try:
        raw_payload = detect_barcode(back_bytes)
    except BarcodeNotFoundError as exc:
        elapsed = int((time.monotonic() - t_start) * 1000)
        logger.warning("barcode_not_found", tried=exc.tried_libraries)
        return VerifyResponse(
            recommendation="REJECT",
            risk_score=1.0,
            hard_fails=["barcode_unreadable"],
            hard_warns=[],
            checks=[],
            extracted_fields=None,
            barcode_detected=False,
            image_quality_passed=image_quality_passed,
            processing_ms=elapsed,
            pipeline_stage_reached="barcode_detection",
            warnings=warnings + [str(exc)],
        )
    except Exception as exc:
        logger.error("barcode_detection_unexpected", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Barcode detection failed unexpectedly. Please try again.",
        ) from exc

    # ── 4. AAMVA parse ────────────────────────────────────────────
    pipeline_stage = "aamva_parse"

    try:
        doc = parse_aamva(raw_payload)
    except Exception as exc:
        elapsed = int((time.monotonic() - t_start) * 1000)
        logger.warning("aamva_parse_failed", error=str(exc))
        return VerifyResponse(
            recommendation="REJECT",
            risk_score=1.0,
            hard_fails=["aamva_parse_failed"],
            hard_warns=[],
            checks=[],
            extracted_fields=None,
            barcode_detected=True,
            image_quality_passed=image_quality_passed,
            processing_ms=int((time.monotonic() - t_start) * 1000),
            pipeline_stage_reached="aamva_parse",
            warnings=warnings + [f"AAMVA parse error: {exc}"],
        )

    # ── 5. Cross-validation checks ─────────────────────────────────
    pipeline_stage = "cross_validation"

    # Validators receive the full ParsedAAMVADocument -- they access
    # whichever of .raw_fields / .normalized_fields they need internally.
    validation_results = [
        check_syntax_conformance(doc),
        check_date_logic(doc),
        check_expiry_window(doc),
        check_jurisdiction_fields(doc),
        check_dcf_entropy(doc),
        check_age_derived_fields(doc),
    ]

    # ── 6. Risk scoring ──────────────────────────────────────────
    pipeline_stage = "risk_scoring"
    score_result = score(validation_results)

    # ── 7. Assemble response ──────────────────────────────────────
    elapsed = int((time.monotonic() - t_start) * 1000)

    response = VerifyResponse(
        recommendation=score_result.recommendation,
        risk_score=score_result.risk_score,
        hard_fails=score_result.fired_hard_fails,
        hard_warns=score_result.fired_hard_warns,
        checks=_build_check_breakdown(validation_results, score_result),
        extracted_fields=_build_extracted_fields(doc),
        barcode_detected=True,
        image_quality_passed=image_quality_passed,
        processing_ms=elapsed,
        pipeline_stage_reached="risk_scoring",
        warnings=warnings,
    )

    logger.info(
        "verify_complete",
        recommendation=response.recommendation,
        risk_score=response.risk_score,
        processing_ms=elapsed,
        hard_fails=response.hard_fails,
    )

    return response
