"""
PDF417 Barcode Detector
=======================
Locates and decodes the PDF417 barcode from a DL back-image.

Strategy (dual-library with preprocessing fallback):
  1. Try zxing-cpp (faster, pure C++ binding, no system dependency)
  2. Fall back to pyzbar (requires libzbar0 installed on the system)
  Each library is tried on up to three image variants:
    a. Original image
    b. Upscaled to 1200 px wide (if smaller)
    c. CLAHE contrast-enhanced

IMPORTANT — format filtering:
  DL cards contain MULTIPLE barcodes: a short 1D symbol (Code 128 / Code 39)
  near the magnetic stripe area AND the large PDF417 on the back. Decoders
  find the 1D barcode first because it is cheaper to decode. We must
  explicitly filter to PDF417 only and enforce a minimum payload length;
  real AAMVA PDF417 payloads are always > 200 characters.

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
# Internal helpers
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


def _try_zxing(img_bgr: np.ndarray) -> str | None:
    """
    Attempt decode with zxing-cpp.

    Returns the PDF417 payload string, or None if no PDF417 barcode
    was found or all candidates were too short.

    NOTE: The 'ambiguous format' fallback (accepting any single barcode
    regardless of format) has been intentionally removed. DL cards contain
    1D barcodes that zxingcpp finds first; accepting them caused the parser
    to receive a 20-char string instead of the 500+ char AAMVA payload.
    """
    try:
        import zxingcpp  # type: ignore

        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        results = zxingcpp.read_barcodes(rgb)

        for r in results:
            fmt = r.format.name.upper().replace("-", "_")
            if fmt in _ZXING_PDF417_FORMATS:
                logger.debug("zxing_hit", format=fmt, payload_len=len(r.text))
                if _is_long_enough(r.text, "zxing-cpp"):
                    return r.text
                # Found PDF417 but payload too short — keep scanning
                # other results before giving up on this variant

        # Log all formats found so operators can debug wrong-barcode issues
        if results:
            logger.debug(
                "zxing_no_pdf417",
                formats_found=[r.format.name for r in results],
                lengths=[len(r.text) for r in results],
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


def _preprocess_variants(img_bgr: np.ndarray) -> list[np.ndarray]:
    """
    Return a list of image variants to try in order.
    Real-world captures often need contrast enhancement or upscaling
    before barcodes decode reliably.
    """
    variants: list[np.ndarray] = [img_bgr]  # original first — cheapest path

    h, w = img_bgr.shape[:2]

    # Upscale if width is below the recommended minimum for PDF417 decode
    if w < 1200:
        scale = 1200 / w
        upscaled = cv2.resize(
            img_bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
        )
        variants.append(upscaled)

    # CLAHE contrast-enhanced variant (helps with low-contrast or faded barcodes)
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced_gray = clahe.apply(gray)
    variants.append(cv2.cvtColor(enhanced_gray, cv2.COLOR_GRAY2BGR))

    return variants


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def detect_barcode(image_bytes: bytes) -> str:
    """
    Locate and decode the PDF417 barcode from raw image bytes.

    Tries zxing-cpp first, then pyzbar, across multiple preprocessed image
    variants (original -> upscaled -> CLAHE-enhanced).

    Only PDF417 results that are >= 100 characters are returned; shorter
    results are treated as wrong-barcode-type reads and discarded.

    Parameters
    ----------
    image_bytes : JPEG or PNG bytes of the DL back image

    Returns
    -------
    str
        Raw barcode payload -- the AAMVA-format plaintext string beginning
        with ``@\n\x1e\rANSI `` or ``@\n\x1e\rAAAA``.

    Raises
    ------
    BarcodeNotFoundError
        Typed exception raised when all library + preprocessing attempts fail.
        ``exc.tried_libraries`` lists which libraries were available and tried.
    """
    arr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise BarcodeNotFoundError(
            "Could not decode image bytes -- unsupported format or corrupted file.",
            tried_libraries=[],
        )

    tried: list[str] = []
    variants = _preprocess_variants(img)

    for variant in variants:
        # -- zxing-cpp -------------------------------------------------------
        payload = _try_zxing(variant)
        if payload:
            if "zxing-cpp" not in tried:
                tried.append("zxing-cpp")
            logger.info(
                "barcode_detected",
                library="zxing-cpp",
                payload_len=len(payload),
            )
            return payload
        if "zxing-cpp" not in tried:
            tried.append("zxing-cpp")

        # -- pyzbar fallback -------------------------------------------------
        payload = _try_pyzbar(variant)
        if payload:
            logger.info(
                "barcode_detected",
                library="pyzbar",
                payload_len=len(payload),
            )
            return payload
        if "pyzbar" not in tried:
            tried.append("pyzbar")

    logger.warning(
        "barcode_not_found",
        tried_libraries=tried,
        variants_attempted=len(variants),
    )
    raise BarcodeNotFoundError(
        "No PDF417 barcode detected in the submitted image after all preprocessing attempts. "
        "Ensure the back of the DL is photographed clearly with the barcode fully visible "
        "and in focus. Common causes: image too small, barcode region blurry, or only the "
        "front of the DL was uploaded instead of the back.",
        tried_libraries=tried,
    )
