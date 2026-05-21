"""
AAMVA Field Parser
==================
Parses the raw AAMVA PDF417 barcode string into a structured dict.

AAMVA 2020 standard:
  https://www.aamva.org/getmedia/99ac7057-0f4d-4461-b0a2-3a5532e1b35c/AAMVA-2020-DLID-Card-Design-Standard.pdf

Barcode format:
  @\n\x1e\rANSI <IIN><AAMVAVersion><JurisdictionVersion><NumberOfEntries>
  DL<RecordLength><ElementId><Value>\n...

Parsing strategy:
  1. Normalize raw payload (strip NUL bytes / control-char prefix artefacts)
  2. Try aamva-barcode-library (handles version negotiation cleanly)
  3. Fall back to hand-rolled regex/line parser for resilience
  4. Normalize all date fields from MMDDCCYY -> YYYY-MM-DD (ISO 8601)
  5. Collect all Z-prefixed jurisdiction-specific fields into a
     dedicated bucket (jurisdiction_fields)

Public API:
  parse_aamva(raw_barcode: str) -> ParsedAAMVADocument
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

from app.utils.logger import logger


# ---------------------------------------------------------------------------
# AAMVA field catalogue
# ---------------------------------------------------------------------------
AAMVA_FIELDS: Dict[str, Dict] = {
    # Mandatory
    "DAQ": {"name": "Customer ID / License Number", "max_len": 25, "required": True},
    "DCS": {"name": "Family Name",                  "max_len": 40, "required": True},
    "DAC": {"name": "First Name",                   "max_len": 40, "required": True},
    "DAD": {"name": "Middle Name/Initial",          "max_len": 40, "required": False},
    "DBB": {"name": "Date of Birth",               "max_len": 8,  "required": True,  "format": "MMDDCCYY"},
    "DBA": {"name": "Document Expiration Date",    "max_len": 8,  "required": True,  "format": "MMDDCCYY"},
    "DBD": {"name": "Document Issue Date",         "max_len": 8,  "required": True,  "format": "MMDDCCYY"},
    "DAG": {"name": "Address - Street 1",           "max_len": 35, "required": True},
    "DAH": {"name": "Address - Street 2",           "max_len": 35, "required": False},
    "DAI": {"name": "Address - City",              "max_len": 20, "required": True},
    "DAJ": {"name": "Address - Jurisdiction Code", "max_len": 2,  "required": True},
    "DAK": {"name": "Address - Postal Code",       "max_len": 11, "required": True},
    "DBC": {"name": "Sex",                          "max_len": 1,  "required": True},
    "DAU": {"name": "Height (FT/IN)",              "max_len": 6,  "required": True},
    "DCF": {"name": "Document Discriminator",      "max_len": 25, "required": True},
    "DCG": {"name": "Country Identification",      "max_len": 3,  "required": True},
    # Optional high-signal
    "DAY": {"name": "Eye Color",                   "max_len": 3,  "required": False},
    "DAZ": {"name": "Hair Color",                  "max_len": 12, "required": False},
    "DAW": {"name": "Weight (lbs)",                "max_len": 3,  "required": False},
    "DDH": {"name": "Under 18 Until",             "max_len": 8,  "required": False, "format": "MMDDCCYY"},
    "DDI": {"name": "Under 19 Until",             "max_len": 8,  "required": False, "format": "MMDDCCYY"},
    "DDJ": {"name": "Under 21 Until",             "max_len": 8,  "required": False, "format": "MMDDCCYY"},
    "DDD": {"name": "Limited Duration Document",  "max_len": 1,  "required": False},
    "DCK": {"name": "Inventory Control Number",   "max_len": 25, "required": False},
    "DBN": {"name": "Alias / AKA Last Name",      "max_len": 10, "required": False},
    "DBS": {"name": "Alias / AKA Suffix",         "max_len": 5,  "required": False},
}

# Element IDs whose values are MMDDCCYY-formatted dates
_DATE_FIELDS = {"DBB", "DBA", "DBD", "DDH", "DDI", "DDJ"}

# Regex: 3-char element ID followed by its value up to the next element ID or end-of-string.
# Minimum value length is 1 char; (.{1,}) avoids the 0-char empty-match edge case
# but the real guard is the {2,} minimum below that prevents single-char truncation
# when consecutive IDs are directly adjacent after delimiter stripping.
#
# BUG FIX: changed (.+?) to (.{1,}?) with re.DOTALL to ensure the lazy quantifier
# doesn't match zero-length values.  The real protection is that Strategy B is only
# used when line-splitting produces <5 fields, and _split_field_section below
# handles the common \x1e-delimited case before we reach the regex.
_FIELD_RE = re.compile(r"([A-Z]{2}[A-Z0-9])(.{1,}?)(?=[A-Z]{2}[A-Z0-9]|\Z)", re.DOTALL)

# AAMVA payloads are always > 200 chars. Anything under 50 is the wrong barcode type.
_AAMVA_MIN_LENGTH = 50

# Control characters that the AAMVA format uses as delimiters and that
# barcode decoders embed in the payload string.
# These must NEVER appear in field values; strip them unconditionally.
_CTRL_CHARS = str.maketrans("", "", "\r\n\x00\x1e\x1d\t")


# ---------------------------------------------------------------------------
# ParsedAAMVADocument  - public return type
# ---------------------------------------------------------------------------

@dataclass
class ParsedAAMVADocument:
    """
    Structured result of parsing an AAMVA PDF417 barcode.

    Attributes
    ----------
    raw_fields : Dict[str, str]
        All extracted element IDs -> raw string values exactly as they appear
        in the barcode (dates in MMDDCCYY, etc.).
        Values are stripped of control characters (\r, \n, \x00, \x1e).

    normalized_fields : Dict[str, str]
        Same as raw_fields but with date values converted to ISO 8601
        (YYYY-MM-DD). Non-date fields are unchanged.

    jurisdiction_fields : Dict[str, str]
        All Z-prefixed state-specific fields (ZAA, ZCA, ZVA, ...).
        These are also present in raw_fields / normalized_fields.

    parse_method : str
        Which parser produced this result: "library" | "fallback"
    """
    raw_fields: Dict[str, str] = field(default_factory=dict)
    normalized_fields: Dict[str, str] = field(default_factory=dict)
    jurisdiction_fields: Dict[str, str] = field(default_factory=dict)
    parse_method: str = "fallback"


# ---------------------------------------------------------------------------
# Value cleaning
# ---------------------------------------------------------------------------

def _clean_value(value: str) -> str:
    """
    Strip all AAMVA delimiter control characters from a field value and
    trim surrounding whitespace.

    The AAMVA PDF417 format uses \n (0x0A) as a field delimiter and
    \x1e / \x1d as record/subfile separators. Barcode decoders embed
    these characters in the decoded string. If they are not stripped
    before storing field values every downstream check fails:

      'NC\n'       -> length 3, fails max_len=2 for DAJ
      '03031974\n' -> fails re.fullmatch(r'\d{8}', ...)
      '1\n'        -> not in {"1","2","9"} for DBC sex code
      'USA\n'      -> != 'USA' for DCG country check
    """
    return value.translate(_CTRL_CHARS).strip()


# ---------------------------------------------------------------------------
# Payload normalisation
# ---------------------------------------------------------------------------

def _normalize_payload(raw: str) -> str:
    """
    Minimal cleanup of barcode decoder artefacts that appear BEFORE the
    AAMVA header -- NUL bytes, leading/trailing whitespace.

    IMPORTANT: we do NOT strip \n, \r, or \x1e globally here.
    Those characters are the field delimiters inside the data section
    and must be preserved so that _fallback_parse can split on them.
    Stripping them globally (old behaviour) caused the entire data
    section to collapse into one unsplit line, triggering Strategy B
    where the lazy (.+?) regex matched only 1 character per field --
    producing truncated values like 'C', 'R', 'N' instead of full names.

    Only NUL bytes (\x00) are stripped globally because they are
    true decoder artefacts with no semantic meaning in any AAMVA field.
    """
    # Remove NUL bytes that some PDF417 readers pad into the output
    cleaned = raw.replace("\x00", "")
    # Strip leading/trailing ASCII whitespace only (not internal)
    return cleaned.strip()


def _looks_like_aamva(text: str) -> bool:
    """
    Return True if *text* contains at least one canonical AAMVA marker.
    """
    markers = ("@", "ANSI", "DAQ", "AAMVA", "DL")
    return any(m in text for m in markers)


# ---------------------------------------------------------------------------
# Date normalization
# ---------------------------------------------------------------------------

def _normalize_date(mmddccyy: str) -> str:
    """
    Convert an AAMVA date string from MMDDCCYY to ISO 8601 YYYY-MM-DD.
    Input is stripped of whitespace and control chars before parsing.
    """
    raw = _clean_value(mmddccyy)  # strip any residual control chars
    if len(raw) != 8 or not raw.isdigit():
        return raw  # pass through unchanged; validators will catch it
    try:
        dt = datetime.strptime(raw, "%m%d%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return raw  # e.g. month=13 -- return raw so validator can flag it


def _apply_date_normalization(fields: Dict[str, str]) -> Dict[str, str]:
    """
    Return a copy of *fields* with all date field values converted to
    ISO 8601. Non-date fields are copied unchanged.
    Values are re-cleaned here as a safety net even if _fallback_parse
    already cleaned them.
    """
    normalized: Dict[str, str] = {}
    for elem_id, value in fields.items():
        clean = _clean_value(value)
        if elem_id in _DATE_FIELDS:
            normalized[elem_id] = _normalize_date(clean)
        else:
            normalized[elem_id] = clean
    return normalized


# ---------------------------------------------------------------------------
# ZXX jurisdiction field extraction
# ---------------------------------------------------------------------------

def _extract_zxx_fields(fields: Dict[str, str]) -> Dict[str, str]:
    """
    Pull all Z-prefixed element IDs into a separate dict.
    """
    return {k: v for k, v in fields.items() if k.startswith("Z")}


# ---------------------------------------------------------------------------
# Library parse strategy
# ---------------------------------------------------------------------------

def _try_aamva_library(raw: str) -> Optional[Dict[str, str]]:
    """Attempt parse using aamva-barcode-library. Returns flat dict or None."""
    try:
        import aamva_barcode_library as aamva  # type: ignore

        doc = aamva.decode(raw)
        result: Dict[str, str] = {}
        for subfile in doc.subfiles:
            for elem_id, value in subfile.elements.items():
                cleaned = _clean_value(str(value))
                if cleaned:
                    result[str(elem_id).strip()] = cleaned
        return result if result else None

    except ImportError:
        logger.warning(
            "aamva_library_not_installed",
            msg="Install aamva-barcode-library for best parse results",
        )
    except Exception as exc:
        logger.warning("aamva_library_parse_error", error=str(exc))

    return None


# ---------------------------------------------------------------------------
# Data section splitter  (handles \n, \r, and \x1e delimiters)
# ---------------------------------------------------------------------------

def _split_field_section(data_section: str) -> list[str]:
    """
    Split the AAMVA data section into individual field lines.

    The AAMVA standard specifies \n (0x0A) as the field record separator.
    However, different PDF417 decoders and card-writer implementations
    use different control characters as field delimiters in practice:

      zxingcpp on AAMVA 2010+  : \n  (most common)
      pyzbar on some encoders  : \r\n
      Some older card writers  : \x1e (ASCII Record Separator, 0x1E)
      Some encoders            : \x1d (ASCII Group Separator, 0x1D)

    This splitter handles all four variants by splitting on any
    combination of [\r\n\x1e\x1d]+.

    BUG FIX: The old code split only on [\r\n]+.  When \x1e was used
    as the field delimiter, all fields collapsed into a single line.
    Strategy B (regex) then ran on the unsplit string, and the lazy
    (.+?) quantifier matched only 1 char per field (e.g. 'C', 'R', 'N')
    instead of the full field value.
    """
    return re.split(r"[\r\n\x1e\x1d]+", data_section)


# ---------------------------------------------------------------------------
# Fallback hand-rolled parser
# ---------------------------------------------------------------------------

def _fallback_parse(raw: str) -> Dict[str, str]:
    """
    Hand-rolled AAMVA parser.

    Handles the standard @\n\x1e\rANSI ... DL subfile line-delimited format
    as well as the compact (no-newline) variant.

    Key fix: AAMVA barcodes use \n (and sometimes \x1e) as the field
    delimiter, so each field is separated by one of these characters.
    The line scanner must strip these delimiters from every value via
    _clean_value().

    Additionally, the DL subfile record begins with 'DL' + a 4-digit
    record length, meaning the first field line looks like:
        'DL0280DAQ123456789\n'
    We must skip the 'DL' prefix and the record-length digits before
    looking for 3-char element IDs.
    """
    fields: Dict[str, str] = {}

    # Locate DL subfile marker; fall back to start of string
    dl_marker = raw.find("DL")
    data_section = raw[dl_marker:] if dl_marker != -1 else raw

    # Strategy A: line-by-line (most common encoding)
    # BUG FIX: split on [\r\n\x1e\x1d]+ to handle all AAMVA delimiter variants.
    # Old code split only on [\r\n]+, missing \x1e-delimited barcodes.
    lines = _split_field_section(data_section)

    for raw_line in lines:
        # Strip all control characters from the line
        line = _clean_value(raw_line)
        if not line:
            continue

        # Skip the 'DL' subfile header line (starts with 'DL' followed
        # by digits, e.g. 'DL0280').
        # Also skip 'ID' subfile headers (some states encode both DL+ID).
        header_match = re.match(r'^(DL|ID)\d*', line)
        if header_match:
            # The remainder after the header prefix may contain the first
            # field immediately -- try to extract it.
            remainder = line[header_match.end():]
            if len(remainder) >= 4 and re.match(r'^[A-Z]{2}[A-Z0-9]', remainder):
                elem_id = remainder[:3]
                value = remainder[3:].strip()
                if elem_id and value:
                    fields[elem_id] = value
            continue

        # Normal field line: must start with a 3-char element ID
        if len(line) >= 4 and re.match(r'^[A-Z]{2}[A-Z0-9]', line):
            elem_id = line[:3]
            value = line[3:].strip()
            if elem_id and value:
                fields[elem_id] = value

    # Strategy B: compact regex scan (fallback when < 5 fields from line scan)
    # Used for barcodes encoded without any line/record delimiters.
    # Only runs if line-splitting found very few fields -- acts as last resort.
    if len(fields) < 5:
        fields.clear()  # discard partial results
        # Strip all remaining control chars before regex scan so element IDs
        # and values are directly adjacent without delimiter noise.
        compact = data_section.translate(_CTRL_CHARS)
        for m in _FIELD_RE.finditer(compact):
            elem_id = m.group(1)
            value = m.group(2).strip()
            if value:
                fields[elem_id] = value

    return fields


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def parse_aamva(raw_barcode: str) -> ParsedAAMVADocument:
    """
    Parse a raw AAMVA PDF417 barcode string.

    Parameters
    ----------
    raw_barcode : the string payload returned by detect_barcode()

    Returns
    -------
    ParsedAAMVADocument
        .raw_fields          -- all element IDs with original values (ctrl chars stripped)
        .normalized_fields   -- same but date fields in YYYY-MM-DD
        .jurisdiction_fields -- Z-prefixed state-specific fields
        .parse_method        -- "library" | "fallback"

    Raises
    ------
    ValueError
        If the payload is too short, fails AAMVA header sanity checks,
        or yields fewer than 3 parseable fields.
    """
    if not raw_barcode:
        raise ValueError("Barcode payload is empty")

    if len(raw_barcode) < _AAMVA_MIN_LENGTH:
        raise ValueError(
            f"Barcode payload is only {len(raw_barcode)} characters -- "
            f"AAMVA PDF417 payloads are always >{_AAMVA_MIN_LENGTH} chars. "
            "This is almost certainly a 1D barcode (Code128/Code39) decoded "
            "instead of the PDF417. Make sure the BACK of the DL is uploaded, "
            "not the front."
        )

    cleaned = _normalize_payload(raw_barcode)

    logger.debug(
        "aamva_header_check",
        preview=cleaned[:120],
        raw_len=len(raw_barcode),
        cleaned_len=len(cleaned),
    )

    if not _looks_like_aamva(cleaned):
        raise ValueError(
            "Barcode payload does not appear to be AAMVA-formatted "
            "(missing expected header markers '@', 'ANSI', 'DAQ', 'AAMVA', or 'DL'). "
            f"Payload length: {len(raw_barcode)} chars. "
            f"Preview (first 80 chars): {repr(cleaned[:80])}"
        )

    # --- Strategy 1: aamva-barcode-library ---
    raw_fields = _try_aamva_library(raw_barcode)
    method = "library"

    # --- Strategy 2: fallback ---
    if not raw_fields or len(raw_fields) < 5:
        raw_fields = _fallback_parse(raw_barcode)
        method = "fallback"

    if len(raw_fields) < 3:
        raise ValueError(
            f"AAMVA parser extracted only {len(raw_fields)} fields -- "
            "barcode may be malformed or non-AAMVA"
        )

    # --- Post-processing ---
    # _apply_date_normalization also re-cleans all values as a safety net
    normalized_fields = _apply_date_normalization(raw_fields)
    jurisdiction_fields = _extract_zxx_fields(raw_fields)

    logger.info(
        "aamva_parse",
        method=method,
        field_count=len(raw_fields),
        zxx_count=len(jurisdiction_fields),
        date_fields_normalized=len(
            [k for k in raw_fields if k in _DATE_FIELDS]
        ),
    )

    return ParsedAAMVADocument(
        raw_fields=raw_fields,
        normalized_fields=normalized_fields,
        jurisdiction_fields=jurisdiction_fields,
        parse_method=method,
    )
