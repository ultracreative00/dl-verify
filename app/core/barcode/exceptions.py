"""
Barcode-layer typed exceptions.
Import these in callers so `except BarcodeNotFoundError` stays narrow.
"""
from __future__ import annotations


class BarcodeNotFoundError(Exception):
    """
    Raised by detect_barcode() when no PDF417 barcode can be located or
    decoded from the supplied image, after all preprocessing variants and
    both library strategies (zxing-cpp + pyzbar) have been exhausted.

    Attributes
    ----------
    message : human-readable explanation suitable for the API error response
    tried_libraries : list of library names that were attempted
    """

    def __init__(
        self,
        message: str = "No PDF417 barcode detected in the submitted image.",
        tried_libraries: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.tried_libraries: list[str] = tried_libraries or []

    def __repr__(self) -> str:
        return (
            f"BarcodeNotFoundError(message={self.message!r}, "
            f"tried_libraries={self.tried_libraries!r})"
        )
