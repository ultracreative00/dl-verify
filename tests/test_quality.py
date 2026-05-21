"""
tests/test_quality.py
=====================
Unit tests for app.core.image.quality.check_image_quality().

All test images are generated synthetically using numpy + cv2 in memory.
No live photos or file I/O required.
"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.core.image.quality import ImageQualityError, ImageQualityResult, check_image_quality


# ---------------------------------------------------------------------------
# Image generation helpers
# ---------------------------------------------------------------------------

def _encode_jpeg(img: np.ndarray, quality: int = 92) -> bytes:
    """Encode a BGR numpy array to JPEG bytes."""
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok, "cv2.imencode failed in test helper"
    return bytes(buf)


def _sharp_card_image(w: int = 900, h: int = 560) -> bytes:
    """
    Sharp synthetic DL-sized image with random texture.
    Laplacian variance will be >> blur threshold.
    """
    rng = np.random.default_rng(42)
    img = rng.integers(80, 200, (h, w, 3), dtype=np.uint8)
    # Add high-frequency edges to ensure high Laplacian variance
    for i in range(0, h, 20):
        cv2.line(img, (0, i), (w, i), (0, 0, 0), 1)
    for j in range(0, w, 20):
        cv2.line(img, (j, 0), (j, h), (255, 255, 255), 1)
    return _encode_jpeg(img)


def _blurry_image(w: int = 900, h: int = 560, blur_ksize: int = 61) -> bytes:
    """Image blurred with a large Gaussian kernel — low Laplacian variance."""
    rng = np.random.default_rng(7)
    img = rng.integers(80, 200, (h, w, 3), dtype=np.uint8)
    img = cv2.GaussianBlur(img, (blur_ksize, blur_ksize), 0)
    return _encode_jpeg(img)


def _overexposed_image(w: int = 900, h: int = 560) -> bytes:
    """Mostly white image — high glare ratio."""
    img = np.full((h, w, 3), 250, dtype=np.uint8)
    return _encode_jpeg(img)


def _low_resolution_image() -> bytes:
    """Image below minimum width/height thresholds."""
    img = np.random.randint(0, 255, (100, 150, 3), dtype=np.uint8)
    return _encode_jpeg(img)


def _corrupted_bytes() -> bytes:
    """Not a valid image — random bytes."""
    return b"THIS IS NOT AN IMAGE " * 10


def _wrong_aspect_image() -> bytes:
    """Square image — 1:1 aspect ratio, outside DL card range."""
    rng = np.random.default_rng(99)
    img = rng.integers(0, 255, (600, 600, 3), dtype=np.uint8)
    return _encode_jpeg(img)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCheckImageQuality:
    def test_sharp_card_passes(self):
        result = check_image_quality(_sharp_card_image(), side="back")
        assert isinstance(result, ImageQualityResult)
        assert result.passed is True
        assert result.score > 50

    def test_passed_result_has_empty_reason(self):
        result = check_image_quality(_sharp_card_image(), side="front")
        assert result.passed is True
        assert result.reason == ""

    def test_blurry_image_fails(self):
        result = check_image_quality(_blurry_image(), side="back")
        assert result.passed is False
        assert result.score <= 35
        assert "blur" in result.reason.lower() or result.reason != ""

    def test_overexposed_image_warns_or_fails(self):
        result = check_image_quality(_overexposed_image(), side="front")
        # Glare may produce passed=False or a low score with reason
        assert result.reason != "" or result.score < 80

    def test_low_resolution_fails(self):
        result = check_image_quality(_low_resolution_image(), side="back")
        assert result.passed is False
        assert result.score <= 35

    def test_corrupted_bytes_fails_gracefully(self):
        # Should return passed=False, not raise an unhandled exception
        result = check_image_quality(_corrupted_bytes(), side="back")
        assert result.passed is False

    def test_wrong_aspect_ratio_warns(self):
        result = check_image_quality(_wrong_aspect_image(), side="front")
        # 1:1 aspect ratio is outside the DL range — should warn
        assert "aspect" in result.reason.lower() or result.score < 100

    def test_result_has_detail_dict(self):
        result = check_image_quality(_sharp_card_image())
        assert isinstance(result.detail, dict)

    def test_score_within_bounds(self):
        for make_img in [_sharp_card_image, _blurry_image, _overexposed_image]:
            result = check_image_quality(make_img())
            assert 0 <= result.score <= 100

    def test_side_parameter_accepted(self):
        result_front = check_image_quality(_sharp_card_image(), side="front")
        result_back = check_image_quality(_sharp_card_image(), side="back")
        assert result_front.passed is True
        assert result_back.passed is True


class TestImageQualityError:
    def test_error_is_exception(self):
        assert issubclass(ImageQualityError, Exception)

    def test_error_can_be_raised_and_caught(self):
        with pytest.raises(ImageQualityError):
            raise ImageQualityError("test error")
