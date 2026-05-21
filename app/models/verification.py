from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Recommendation(str, Enum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


class SignalStatus(str, Enum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    FAIL = "FAIL"
    SKIP = "SKIP"


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class SignalResult(BaseModel):
    """Output of a single cross-validation check."""
    passed: bool
    score: float = Field(ge=0.0, le=1.0, description="1.0 = fully passing, 0.0 = hard fail")
    detail: Dict[str, Any] = Field(default_factory=dict)
    message: Optional[str] = None


class QualityGateResult(BaseModel):
    passed: bool
    score: int = Field(ge=0, le=100)
    blur_value: Optional[float] = None
    glare_ratio: Optional[float] = None
    resolution: Optional[str] = None
    size_mb: Optional[float] = None
    detail: Dict[str, Any] = Field(default_factory=dict)
    message: Optional[str] = None


class ExtractedFields(BaseModel):
    """Normalised fields extracted from the AAMVA barcode (and optionally OCR)."""
    # Identity
    first_name: Optional[str] = None
    middle_name: Optional[str] = None
    last_name: Optional[str] = None
    dob: Optional[str] = None           # YYYY-MM-DD
    sex: Optional[str] = None           # M | F | NS

    # Document
    dl_number: Optional[str] = None
    expiration: Optional[str] = None    # YYYY-MM-DD
    issue_date: Optional[str] = None    # YYYY-MM-DD
    state: Optional[str] = None
    country: Optional[str] = None
    document_discriminator: Optional[str] = None

    # Physical descriptors
    height: Optional[str] = None
    eye_color: Optional[str] = None
    hair_color: Optional[str] = None
    weight_lbs: Optional[str] = None

    # Address
    address_street: Optional[str] = None
    address_city: Optional[str] = None
    address_state: Optional[str] = None
    address_zip: Optional[str] = None

    # Age-derived
    under_18_until: Optional[str] = None  # DDH field, YYYY-MM-DD
    limited_duration: Optional[bool] = None  # DDD field

    # Raw jurisdiction-specific fields (ZXX)
    jurisdiction_fields: Dict[str, str] = Field(default_factory=dict)

    # Source attribution: "barcode" | "ocr" | "merged"
    source: str = "barcode"


class OcrDiffField(BaseModel):
    """Per-field comparison result from OCR ↔ barcode diff."""
    field: str
    barcode_value: Optional[str] = None
    ocr_value: Optional[str] = None
    match: bool
    similarity: float = Field(ge=0.0, le=1.0)
    detail: Optional[str] = None


class OcrBarcodesDiff(BaseModel):
    overall_match: bool
    mismatch_count: int
    fields: List[OcrDiffField] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level response
# ---------------------------------------------------------------------------

class VerificationResponse(BaseModel):
    session_id: str
    recommendation: Recommendation
    risk_score: float = Field(ge=0.0, le=1.0, description="0 = clean, 1 = certain fraud")
    processing_ms: int
    created_at: datetime = Field(default_factory=datetime.utcnow)

    quality_gate: Optional[QualityGateResult] = None
    barcode_parsed_fields: Optional[Dict[str, str]] = None  # raw AAMVA key → value
    extracted_fields: Optional[ExtractedFields] = None      # normalised

    cross_validation_signals: Dict[str, SignalResult] = Field(default_factory=dict)

    # Sprint 2
    ocr_fields: Optional[ExtractedFields] = None
    ocr_barcode_diff: Optional[OcrBarcodesDiff] = None
