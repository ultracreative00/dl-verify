"""
PDF417 Barcode Detector
=======================
Locates and decodes the PDF417 barcode from a DL back-image.

Strategy (dual-library fallback):
  1. Try zxing-cpp (faster, pure C++ binding, no system dependency)
  2. Fall back to pyzbar (requires libzbar0 installed on the system)

Returns the raw barcode string payload, or raises RuntimeError if not found.
"""
from __future__ import annotations

import numpy as np
import cv2

from app.utils.logger import logger


def _try_zxing(img_bgr: np.ndarray) -> str | None:
    """Attempt decode with zxing-cpp."""
    try:
        import zxingcpp  # type: ignore
        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        results = zxingcpp.read_barcodes(rgb)
        for r in results:
            if "PDF" in r.format.name.upper() or r.format.name.upper() == "PDF_417":
                return r.text
        # Accept any result if only one found and PDF417 not identified by format
        if len(results) == 1:
            return results[0].text
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("zxing_decode_error", error=str(exc))
    return None


def _try_pyzbar(img_bgr: np.ndarray) -> str | None:
    """Attempt decode with pyzbar (requires libzbar0)."""
    try:
        from pyzbar.pyzbar import decode as pyzbar_decode, ZBarSymbol  # type: ignore
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        results = pyzbar_decode(gray, symbols=[ZBarSymbol.PDF417])
        if results:
            return results[0].data.decode("utf-8", errors="replace")
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("pyzbar_decode_error", error=str(exc))
    return None


def _preprocess_variants(img_bgr: np.ndarray) -> list[np.ndarray]:
    """
    Return a list of image variants to try in order.
    Some real-world captures need contrast enhancement before barcodes decode.
    """
    variants = [img_bgr]  # original first

    # Upscale if small
    h, w = img_bgr.shape[:2]
    if w < 1200:
        scale = 1200 / w
        upscaled = cv2.resize(img_bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        variants.append(upscaled)

    # CLAHE contrast-enhanced grayscale → convert back to BGR for consistency
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    variants.append(cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR))

    return variants


def detect_barcode(image_bytes: bytes) -> str:
    """
    Locate and decode the PDF417 barcode from raw image bytes.

    Parameters
    ----------
    image_bytes : JPEG or PNG bytes

    Returns
    -------
    str : raw barcode payload (the AAMVA string)

    Raises
    ------
    RuntimeError : if no decodable barcode is found after all attempts
    """
    arr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("Could not decode image bytes for barcode detection")

    for variant in _preprocess_variants(img):
        payload = _try_zxing(variant)
        if payload:
            logger.info("barcode_detected", library="zxing", payload_len=len(payload))
            return payload

        payload = _try_pyzbar(variant)
        if payload:
            logger.info("barcode_detected", library="pyzbar", payload_len=len(payload))
            return payload

    raise RuntimeError(
        "No PDF417 barcode detected in the submitted image. "
        "Ensure the back of the DL is photographed clearly with the barcode visible."
    )
