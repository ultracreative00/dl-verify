"""
app.core.image.quality
======================
Public adapter that verify.py (and any future route) imports for
image quality gating.

The actual CV logic lives in app.core.quality.gate (run_quality_gate).
This module wraps it behind the interface expected by the route:

    check_image_quality(image_bytes, side) -> ImageQualityResult
    ImageQualityError                      -- raised on unexpected failures

Why an adapter instead of importing gate.py directly?
- verify.py was written against app.core.image.quality — the canonical
  path implied by the project layout (core/image/* for image concerns).
- app.core.quality.gate already exists with working CV logic and its
  own QualityGateResult / settings wiring.
- This shim lets both coexist without touching either file, and keeps
  the route import path stable for future callers.

ImageQualityResult fields
-------------------------
    passed  : bool   — True = proceed, False = reject early
    reason  : str    — human-readable explanation (empty string if passed)
    score   : int    — 0–100 composite quality score
    detail  : dict   — per-sub-check raw values (blur, glare, resolution …)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class ImageQualityError(Exception):
    """
    Raised when the quality gate itself fails unexpectedly
    (e.g. OpenCV not installed, corrupted image before size check).
    Distinct from a *failed* quality check (passed=False) — that is
    a normal, handled outcome returned as ImageQualityResult(passed=False).
    """


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ImageQualityResult:
    """
    Result of check_image_quality().

    Attributes
    ----------
    passed  : whether the image meets minimum quality thresholds
    reason  : plain-English explanation when passed=False; empty when passed=True
    score   : 0–100 composite quality score (100 = perfect, 0 = unusable)
    detail  : raw sub-check values surfaced in the API response
    """
    passed: bool
    reason: str = ""
    score: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def check_image_quality(image_bytes: bytes, side: str = "back") -> ImageQualityResult:
    """
    Run the image quality gate on raw JPEG/PNG bytes.

    Parameters
    ----------
    image_bytes : raw bytes of the uploaded image
    side        : "front" | "back" — used for logging context only

    Returns
    -------
    ImageQualityResult
        .passed = True  → image acceptable, continue pipeline
        .passed = False → early-reject; .reason explains why

    Raises
    ------
    ImageQualityError
        Only on unexpected internal errors (not on a routine quality
        failure, which is returned as passed=False).
    """
    try:
        from app.core.quality.gate import run_quality_gate  # local import avoids circular dep
        gate_result = run_quality_gate(image_bytes, side=side)
    except ImportError as exc:
        raise ImageQualityError(
            "OpenCV (cv2) is required for image quality checks. "
            "Install it with: pip install opencv-python-headless"
        ) from exc
    except Exception as exc:
        # Surface unexpected errors as ImageQualityError so the route
        # can handle them gracefully (warn + continue) rather than 500.
        raise ImageQualityError(str(exc)) from exc

    reason = gate_result.message if not gate_result.passed else ""

    return ImageQualityResult(
        passed=gate_result.passed,
        reason=reason,
        score=gate_result.score,
        detail=gate_result.detail,
    )
