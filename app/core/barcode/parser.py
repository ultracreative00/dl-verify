"""
AAMVA Field Parser
==================
Parses the raw AAMVA PDF417 barcode string into a structured dict.

AAMVA 2020 standard field definitions:
  https://www.aamva.org/getmedia/99ac7057-0f4d-4461-b0a2-3a5532e1b35c/AAMVA-2020-DLID-Card-Design-Standard.pdf

The barcode format:
  @\n\x1e\rANSI <IIN><AAMVAVersion><JurisdictionVersion><NumberOfEntries>
  DL<RecordLength><ElementId><Value>\n...

This parser:
  1. Tries aamva-barcode-library first (handles version negotiation)
  2. Falls back to a hand-rolled regex parser for resilience
  3. Returns Dict[str, str] of element_id -> raw_value
"""
from __future__ import annotations

import re
from typing import Dict, Optional

from app.utils.logger import logger

# ---------------------------------------------------------------------------
# AAMVA mandatory field catalogue
# Used for validation metadata — NOT exhaustive (ZXX fields are open-ended)
# ---------------------------------------------------------------------------
AAMVA_FIELDS: Dict[str, Dict] = {
    # --- Mandatory fields ---
    "DAQ": {"name": "Customer ID / License Number", "max_len": 25, "required": True},
    "DCS": {"name": "Family Name", "max_len": 40, "required": True},
    "DAC": {"name": "First Name", "max_len": 40, "required": True},
    "DAD": {"name": "Middle Name/Initial", "max_len": 40, "required": False},
    "DBB": {"name": "Date of Birth", "max_len": 8, "required": True, "format": "MMDDCCYY"},
    "DBA": {"name": "Document Expiration Date", "max_len": 8, "required": True, "format": "MMDDCCYY"},
    "DBD": {"name": "Document Issue Date", "max_len": 8, "required": True, "format": "MMDDCCYY"},
    "DAG": {"name": "Address — Street 1", "max_len": 35, "required": True},
    "DAH": {"name": "Address — Street 2", "max_len": 35, "required": False},
    "DAI": {"name": "Address — City", "max_len": 20, "required": True},
    "DAJ": {"name": "Address — Jurisdiction Code", "max_len": 2, "required": True},
    "DAK": {"name": "Address — Postal Code", "max_len": 11, "required": True},
    "DBC": {"name": "Sex", "max_len": 1, "required": True},  # 1=M, 2=F, 9=NS
    "DAU": {"name": "Height (FT/IN)", "max_len": 6, "required": True},
    "DCF": {"name": "Document Discriminator", "max_len": 25, "required": True},
    "DCG": {"name": "Country Identification", "max_len": 3, "required": True},
    # --- Optional but high-signal ---
    "DAY": {"name": "Eye Color", "max_len": 3, "required": False},
    "DAZ": {"name": "Hair Color", "max_len": 12, "required": False},
    "DAW": {"name": "Weight (lbs)", "max_len": 3, "required": False},
    "DDH": {"name": "Under 18 Until", "max_len": 8, "required": False, "format": "MMDDCCYY"},
    "DDI": {"name": "Under 19 Until", "max_len": 8, "required": False, "format": "MMDDCCYY"},
    "DDJ": {"name": "Under 21 Until", "max_len": 8, "required": False, "format": "MMDDCCYY"},
    "DDD": {"name": "Limited Duration Document", "max_len": 1, "required": False},
    "DCK": {"name": "Inventory Control Number", "max_len": 25, "required": False},
    "DBN": {"name": "Alias / AKA Last Name", "max_len": 10, "required": False},
    "DBS": {"name": "Alias / AKA Suffix", "max_len": 5, "required": False},
}

# Regex to extract element id + value pairs from the flat AAMVA string
_FIELD_RE = re.compile(r"([A-Z]{2}[A-Z0-9])(.+?)(?=[A-Z]{2}[A-Z0-9]|\Z)", re.DOTALL)


def _try_aamva_library(raw: str) -> Optional[Dict[str, str]]:
    """Attempt parse using aamva-barcode-library."""
    try:
        import aamva_barcode_library as aamva  # type: ignore
        doc = aamva.decode(raw)
        # Library returns an object with .subfiles[0].elements -> {id: value}
        result: Dict[str, str] = {}
        for subfile in doc.subfiles:
            for elem_id, value in subfile.elements.items():
                result[str(elem_id).strip()] = str(value).strip()
        return result if result else None
    except ImportError:
        logger.warning("aamva_library_not_installed", msg="Install aamva-barcode-library for best results")
    except Exception as exc:
        logger.warning("aamva_library_parse_error", error=str(exc))
    return None


def _fallback_parse(raw: str) -> Dict[str, str]:
    """
    Hand-rolled AAMVA parser.
    Handles the standard @\n\x1e\rANSI ... DL subfile format.
    """
    fields: Dict[str, str] = {}

    # Locate the start of DL subfile data
    # AAMVA header ends after the jurisdictionVersionNumber
    # Data records begin with element IDs (3-char uppercase/digit codes)
    # We search for the DL marker first
    dl_marker = raw.find("DL")
    if dl_marker == -1:
        # Some encoders omit DL marker; try to find first 3-char field directly
        dl_marker = 0

    data_section = raw[dl_marker:]

    # Extract all 3-char element IDs followed by their value
    # Values are terminated by newlines or the next element ID
    lines = re.split(r"\r?\n", data_section)
    for line in lines:
        line = line.strip()
        if len(line) >= 4 and re.match(r"^[A-Z]{2}[A-Z0-9]", line):
            elem_id = line[:3]
            value = line[3:].strip()
            if elem_id and value:
                fields[elem_id] = value

    # Also try the compact form (no newlines between fields)
    if len(fields) < 5:
        for m in _FIELD_RE.finditer(data_section):
            elem_id = m.group(1)
            value = m.group(2).strip().rstrip("\r\n")
            if value:
                fields[elem_id] = value

    return fields


def parse_aamva(raw_barcode: str) -> Dict[str, str]:
    """
    Parse a raw AAMVA PDF417 barcode string into a flat dict.

    Parameters
    ----------
    raw_barcode : the string payload from the PDF417 symbology decoder

    Returns
    -------
    Dict[str, str] : element_id -> raw_value  (e.g. {"DAQ": "D1234567", ...})

    Raises
    ------
    ValueError : if the string is clearly not an AAMVA document
    """
    if not raw_barcode or len(raw_barcode) < 20:
        raise ValueError("Barcode payload too short to be a valid AAMVA document")

    # Basic sanity: AAMVA barcodes start with '@' and contain 'ANSI '
    if "@" not in raw_barcode and "ANSI" not in raw_barcode and "DAQ" not in raw_barcode:
        raise ValueError(
            "Barcode payload does not appear to be an AAMVA-formatted string "
            "(missing expected header markers)"
        )

    # Try library first
    fields = _try_aamva_library(raw_barcode)
    if fields and len(fields) >= 5:
        logger.info("aamva_parse", method="library", field_count=len(fields))
        return fields

    # Fallback
    fields = _fallback_parse(raw_barcode)
    logger.info("aamva_parse", method="fallback", field_count=len(fields))

    if len(fields) < 3:
        raise ValueError(
            f"AAMVA parser extracted only {len(fields)} fields — "
            "barcode may be malformed or non-AAMVA"
        )

    return fields
