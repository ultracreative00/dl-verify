"""
PDF417 Barcode Detector
=======================
Locates and decodes the PDF417 barcode from a DL back-image.

Strategy (dual-library with 8-variant preprocessing cascade):
  1. Try zxing-cpp (faster, pure C++ binding, no system dependency)
  2. Fall back to pyzbar (requires libzbar0 installed on the system)

  Each library is tried across 8 image variants in order of cost:
    1. Original image
    2. Upscaled to 1600 px wide
    3. CLAHE contrast-enhanced (full image)
    4. Bottom-half crop  (PDF417 lives in the bottom half of every US DL)
    5. Bottom-third crop + 2x upscale (most aggressive barcode isolation)
    6. Otsu binarization (handles washed-out / overexposed images)
    7. Adaptive threshold (handles uneven lighting / shadows)
    8. Sharpening kernel  (helps with slight blur / phone camera softness)

  After all PDF417-specific attempts fail, a final last-resort pass tries
  zxing-cpp accepting any format with payload >= MIN_LENGTH.

IMPORTANT — format filtering:
  DL cards contain MULTIPLE barcodes: a short 1D symbol (Code 128 / Code 39)
  near the magnetic stripe AND the large PDF417 on the back. We explicitly
  filter to PDF417 only and enforce a minimum payload length for all normal
  passes; real AAMVA PDF417 payloads are always > 200 characters.

Raises BarcodeNotFoundError (typed) if all attempts fail.
"""
from __future__ import annotations

import numpy as np
import cv2

from app.core.barcode.exceptions import BarcodeNotFoundError
from app.utils.logger import logger

# zxing-cpp format names that indicate a PDF417 symbol
_ZXING_PDF417_FORMATS = {"PDF417", "PDF_417"}

# AAMVA PDF417 payloads are always well over 200 chars.
# Anything shorter is either a 1D barcode or a corrupted read.
_AAMVA_MIN_PAYLOAD_LEN = 100


# ---------------------------------------------------------------------------
# Payload length guard
# ---------------------------------------------------------------------------

def _is_long_enough(payload: str, library: str) -> bool:
    """Return True if the payload meets the AAMVA minimum length threshold."""
    if len(payload) < _AAMVA_MIN_PAYLOAD_LEN:
        logger.warning(
            "barcode_payload_too_short",
            library=library,
            payload_len=len(payload),
            min_expected=_AAMVA_MIN_PAYLOAD_LEN,
            hint="Likely a 1D barcode (Code128/Code39) decoded instead of PDF417",
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Decoder wrappers
# ---------------------------------------------------------------------------

def _try_zxing(img_bgr: np.ndarray, *, any_format: bool = False) -> str | None:
    """
    Attempt decode with zxing-cpp.

    Parameters
    ----------
    any_format : if True, accept any barcode format with payload >= MIN_LENGTH.
                 Used only as a last-resort pass after all PDF417-specific
                 attempts have failed.
    """
    try:
        import zxingcpp  # type: ignore

        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        results = zxingcpp.read_barcodes(rgb)

        for r in results:
            fmt = r.format.name.upper().replace("-", "_")
            is_pdf417 = fmt in _ZXING_PDF417_FORMATS

            if is_pdf417 or any_format:
                logger.debug(
                    "zxing_candidate",
                    format=fmt,
                    payload_len=len(r.text),
                    any_format=any_format,
                )
                if _is_long_enough(r.text, "zxing-cpp"):
                    return r.text

        if results:
            logger.debug(
                "zxing_no_usable_result",
                formats_found=[r.format.name for r in results],
                lengths=[len(r.text) for r in results],
                any_format=any_format,
            )

    except ImportError:
        logger.debug("zxing_not_installed")
    except Exception as exc:
        logger.warning("zxing_decode_error", error=str(exc))

    return None


def _try_pyzbar(img_bgr: np.ndarray) -> str | None:
    """Attempt decode with pyzbar (requires libzbar0). Returns payload string or None."""
    try:
        from pyzbar.pyzbar import decode as pyzbar_decode, ZBarSymbol  # type: ignore

        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        results = pyzbar_decode(gray, symbols=[ZBarSymbol.PDF417])
        if results:
            payload = results[0].data.decode("utf-8", errors="replace")
            logger.debug("pyzbar_hit", payload_len=len(payload))
            if _is_long_enough(payload, "pyzbar"):
                return payload

    except ImportError:
        logger.debug("pyzbar_not_installed")
    except Exception as exc:
        logger.warning("pyzbar_decode_error", error=str(exc))

    return None


# ---------------------------------------------------------------------------
# Preprocessing variants
# ---------------------------------------------------------------------------

def _preprocess_variants(img_bgr: np.ndarray) -> list[np.ndarray]:
    """
    Return a list of image variants to try in order of decode likelihood
    for a real-world DL back photo.

    Variant order (cheapest / most likely first):
      1. Original
      2. Upscaled to 1600px wide  (raised from 1200 -- PDF417 needs density)
      3. CLAHE contrast-enhanced  (full image)
      4. Bottom-half crop         (PDF417 is always in the bottom 50% of a DL)
      5. Bottom-third crop + 2x   (most isolated / highest barcode density)
      6. Otsu binarization        (washed-out / overexposed photos)
      7. Adaptive threshold       (uneven lighting, shadows across card)
      8. Sharpening kernel        (slight motion blur / phone camera softness)
    """
    h, w = img_bgr.shape[:2]
    variants: list[np.ndarray] = []

    # 1. Original
    variants.append(img_bgr)

    # 2. Upscale to 1600px wide (only if smaller)
    if w < 1600:
        scale = 1600 / w
        upscaled = cv2.resize(
            img_bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
        )
        variants.append(upscaled)

    # 3. CLAHE on full image
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    clahe_gray = clahe.apply(gray)
    variants.append(cv2.cvtColor(clahe_gray, cv2.COLOR_GRAY2BGR))

    # 4. Bottom-half crop
    #    PDF417 on a standard AAMVA DL is always in the bottom portion.
    #    Cropping removes header graphics that confuse the decoder.
    bottom_half = img_bgr[h // 2 :, :]
    variants.append(bottom_half)

    # 5. Bottom-third crop + 2x upscale
    #    Most aggressive isolation: just the barcode zone.
    bottom_third = img_bgr[int(h * 0.55) :, :]
    bh, bw = bottom_third.shape[:2]
    if bw > 0 and bh > 0:
        bottom_third_up = cv2.resize(
            bottom_third, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC
        )
        variants.append(bottom_third_up)

    # 6. Otsu binarization (global threshold -- good for uniform backgrounds)
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(cv2.cvtColor(otsu, cv2.COLOR_GRAY2BGR))

    # 7. Adaptive threshold (local threshold -- good for uneven lighting)
    adaptive = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=11,
        C=2,
    )
    variants.append(cv2.cvtColor(adaptive, cv2.COLOR_GRAY2BGR))

    # 8. Sharpening kernel
    sharpen_kernel = np.array(
        [[ 0, -1,  0],
         [-1,  5, -1],
         [ 0, -1,  0]],
        dtype=np.float32,
    )
    sharpened = cv2.filter2D(img_bgr, -1, sharpen_kernel)
    variants.append(sharpened)

    return variants


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def detect_barcode(image_bytes: bytes) -> str:
    """
    Locate and decode the PDF417 barcode from raw image bytes.

    Tries zxing-cpp then pyzbar across 8 preprocessed image variants.
    Only PDF417 results >= 100 characters are returned in normal passes.
    A final last-resort pass accepts any long barcode format from zxingcpp.

    Parameters
    ----------
    image_bytes : JPEG or PNG bytes of the DL back image

    Returns
    -------
    str
        Raw AAMVA barcode payload (500+ chars, begins with @/ANSI header).

    Raises
    ------
    BarcodeNotFoundError
        Raised when all library + preprocessing + last-resort attempts fail.
    """
    arr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise BarcodeNotFoundError(
            "Could not decode image bytes -- unsupported format or corrupted file.",
            tried_libraries=[],
        )

    h, w = img.shape[:2]
    logger.info("barcode_detection_start", image_w=w, image_h=h, image_bytes=len(image_bytes))

    tried: list[str] = []
    variants = _preprocess_variants(img)

    for idx, variant in enumerate(variants):
        vh, vw = variant.shape[:2]
        logger.debug("trying_variant", variant_index=idx, w=vw, h=vh)

        # -- zxing-cpp (PDF417 only) ----------------------------------------
        payload = _try_zxing(variant, any_format=False)
        if payload:
            if "zxing-cpp" not in tried:
                tried.append("zxing-cpp")
            logger.info(
                "barcode_detected",
                library="zxing-cpp",
                variant_index=idx,
                payload_len=len(payload),
            )
            return payload
        if "zxing-cpp" not in tried:
            tried.append("zxing-cpp")

        # -- pyzbar (PDF417 only) -------------------------------------------
        payload = _try_pyzbar(variant)
        if payload:
            logger.info(
                "barcode_detected",
                library="pyzbar",
                variant_index=idx,
                payload_len=len(payload),
            )
            return payload
        if "pyzbar" not in tried:
            tried.append("pyzbar")

    # -------------------------------------------------------------------------
    # Last-resort pass: zxing-cpp accepting any format with payload >= MIN_LENGTH
    # This handles edge cases where zxing-cpp labels a PDF417 with an
    # unexpected format name (seen in some build variants).
    # -------------------------------------------------------------------------
    logger.warning(
        "barcode_pdf417_pass_failed",
        msg="All PDF417-specific attempts failed. Trying last-resort any-format pass.",
        variants_attempted=len(variants),
    )

    for idx, variant in enumerate(variants):
        payload = _try_zxing(variant, any_format=True)
        if payload:
            logger.info(
                "barcode_detected_last_resort",
                library="zxing-cpp",
                variant_index=idx,
                payload_len=len(payload),
            )
            return payload

    logger.warning(
        "barcode_not_found",
        tried_libraries=tried,
        variants_attempted=len(variants),
    )
    raise BarcodeNotFoundError(
        "No PDF417 barcode detected after all preprocessing attempts. "
        "Troubleshooting checklist: "
        "(1) Upload the BACK of the DL, not the front. "
        "(2) Ensure the full barcode is visible and not cut off. "
        "(3) Image should be at least 800px wide -- zoom in on the card if needed. "
        "(4) Avoid extreme glare, shadows, or motion blur over the barcode area. "
        "(5) JPEG quality should be 70+ -- heavily compressed images lose barcode detail.",
        tried_libraries=tried,
    )
