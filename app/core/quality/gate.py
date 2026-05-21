"""
Image Quality Gate
==================
Fast pre-flight check before any expensive barcode or OCR processing.

Checks (in order):
  1. File size within configured limit
  2. Image decodable (not corrupted / unsupported format)
  3. Minimum resolution (width x height)
  4. Sharpness — Laplacian variance (blur detection)
  5. Glare / overexposure — ratio of high-luma pixels
  6. Aspect ratio plausibility (DL cards are ~1.59:1)

Returns a QualityGateResult.  passed=False short-circuits the pipeline.
"""
from __future__ import annotations

from typing import Tuple

import cv2
import numpy as np

from app.models.verification import QualityGateResult
from app.utils.config import get_settings
from app.utils.logger import logger

settings = get_settings()

# DL card aspect ratio range (accounts for perspective/capture angle distortion)
_ASPECT_LOW = 1.20
_ASPECT_HIGH = 2.25


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _decode(image_bytes: bytes) -> np.ndarray:
    arr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("cv2 could not decode image — unsupported format or corrupted file")
    return img


def _laplacian_variance(gray: np.ndarray) -> float:
    """Higher = sharper. Severely blurred images score < 30."""
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _glare_ratio(img: np.ndarray) -> float:
    """Fraction of pixels with luminance >= 240 (overexposed)."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    v_channel = hsv[:, :, 2]
    return float(np.sum(v_channel >= 240) / v_channel.size)


def _resolution(img: np.ndarray) -> Tuple[int, int]:
    h, w = img.shape[:2]
    return w, h


def _aspect(w: int, h: int) -> float:
    return max(w, h) / min(w, h) if min(w, h) > 0 else 0.0


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def run_quality_gate(image_bytes: bytes, side: str = "back") -> QualityGateResult:
    """
    Parameters
    ----------
    image_bytes : raw JPEG / PNG bytes
    side        : "front" | "back" — used for log context only

    Returns
    -------
    QualityGateResult
        passed=True  → image acceptable, proceed to barcode/OCR
        passed=False → reject early, return error to caller
    """
    detail: dict = {}
    issues: list[str] = []
    warnings: list[str] = []
    sub: dict[str, int] = {}

    # --- 1. File size ---
    size_mb = round(len(image_bytes) / (1024 * 1024), 3)
    detail["size_mb"] = size_mb
    if size_mb > settings.max_image_size_mb:
        return QualityGateResult(
            passed=False,
            score=0,
            size_mb=size_mb,
            detail=detail,
            message=f"Image exceeds {settings.max_image_size_mb} MB limit (got {size_mb} MB)",
        )

    # --- 2. Decode ---
    try:
        img = _decode(image_bytes)
    except ValueError as exc:
        return QualityGateResult(
            passed=False,
            score=0,
            size_mb=size_mb,
            detail=detail,
            message=str(exc),
        )

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    w, h = _resolution(img)
    resolution_str = f"{w}x{h}"
    detail["resolution"] = resolution_str

    # --- 3. Resolution ---
    if w < settings.min_image_width or h < settings.min_image_height:
        sub["resolution"] = 10
        issues.append(
            f"Resolution too low: {resolution_str} "
            f"(min {settings.min_image_width}x{settings.min_image_height})"
        )
    else:
        sub["resolution"] = 100

    # --- 4. Blur ---
    blur_val = round(_laplacian_variance(gray), 2)
    detail["blur_value"] = blur_val
    if blur_val < settings.blur_threshold_fail:
        sub["blur"] = 0
        issues.append(f"Image too blurry (sharpness={blur_val}, threshold={settings.blur_threshold_fail})")
    elif blur_val < settings.blur_threshold_warn:
        sub["blur"] = 50
        warnings.append(f"Image marginally blurry (sharpness={blur_val})")
    else:
        sub["blur"] = 100

    # --- 5. Glare ---
    glare = round(_glare_ratio(img), 4)
    detail["glare_ratio"] = glare
    if glare > settings.glare_pixel_ratio:
        sub["glare"] = max(0, int(100 - (glare * 800)))
        warnings.append(f"Glare detected ({glare * 100:.1f}% overexposed pixels)")
    else:
        sub["glare"] = 100

    # --- 6. Aspect ratio ---
    asp = round(_aspect(w, h), 2)
    detail["aspect_ratio"] = asp
    if not (_ASPECT_LOW <= asp <= _ASPECT_HIGH):
        sub["aspect"] = 40
        warnings.append(f"Unusual aspect ratio {asp} (expected {_ASPECT_LOW}–{_ASPECT_HIGH}) — card may be cropped or rotated")
    else:
        sub["aspect"] = 100

    # --- Aggregate score ---
    score = int(
        sub.get("resolution", 100) * 0.30
        + sub.get("blur", 100) * 0.40
        + sub.get("glare", 100) * 0.20
        + sub.get("aspect", 100) * 0.10
    )
    detail["sub_scores"] = sub

    passed = len(issues) == 0
    if not passed:
        score = min(score, 35)  # hard cap when blocking issues exist

    message = "; ".join(issues + warnings) if (issues or warnings) else "Image quality acceptable"

    logger.info(
        "quality_gate",
        side=side,
        score=score,
        passed=passed,
        blur=blur_val,
        glare=glare,
        resolution=resolution_str,
    )

    return QualityGateResult(
        passed=passed,
        score=score,
        blur_value=blur_val,
        glare_ratio=glare,
        resolution=resolution_str,
        size_mb=size_mb,
        detail=detail,
        message=message,
    )
