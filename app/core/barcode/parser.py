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
  3. Fall back to hand-rolled line parser (Strategy A) — split on AAMVA
     field delimiters [\r\n\x1e\x1d] and extract 3-char element IDs.
  4. Only if Strategy A yields < 5 fields, run Strategy B: a known-ID-
     anchored regex scan on the compacted (delimiter-stripped) payload.
     Strategy B uses an alternation of all known AAMVA element IDs as
     the split point so it NEVER mis-splits on accidental 3-char letter
     sequences inside field values (the previous lazy-quantifier bug).
  5. Normalize all date fields from MMDDCCYY -> YYYY-MM-DD (ISO 8601)
  6. Collect all Z-prefixed jurisdiction-specific fields into a
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

# AAMVA payloads are always > 200 chars. Anything under 50 is the wrong barcode type.
_AAMVA_MIN_LENGTH = 50

# All control characters used as AAMVA field/record delimiters.
# These must NEVER appear in stored field values.
_CTRL_CHARS = str.maketrans("", "", "\r\n\x00\x1e\x1d\t")

# ---------------------------------------------------------------------------
# Strategy B: known-ID-anchored regex for compact (no-delimiter) payloads
#
# Build a single alternation of every known element ID so the lookahead
# only splits on REAL AAMVA element IDs, never on accidental 3-char
# uppercase sequences that happen to appear inside field values.
#
# This fixes the critical lazy-quantifier truncation bug where:
#   DCSCOOPER -> DCS='C' because 'OOP' matched [A-Z]{2}[A-Z0-9]
#
# The alternation is sorted longest-first (all are 3 chars, so order
# is alphabetical for determinism) and the lookahead requires the ID
# to be followed by at least one non-empty character or end-of-string.
# ---------------------------------------------------------------------------

def _build_field_re() -> re.Pattern:
    """
    Build the Strategy B regex anchored on known AAMVA element IDs.

    Pattern structure:
        (KNOWN_ID)(.*?)(?=KNOWN_ID|\Z)

    where KNOWN_ID is an alternation of all element IDs in AAMVA_FIELDS
    plus the generic Z-prefixed jurisdiction field pattern Z[A-Z0-9]{2}.

    Using a known-ID alternation instead of the generic [A-Z]{2}[A-Z0-9]
    lookahead prevents false splits on value characters.
    """
    known_ids = sorted(AAMVA_FIELDS.keys())  # deterministic order
    # Also match Z-prefixed jurisdiction fields (ZCA, ZTZ, ZNB, etc.)
    id_pattern = "|".join(re.escape(k) for k in known_ids) + "|Z[A-Z0-9]{2}"
    # Full pattern: capture the element ID, then lazily capture the value
    # up to the next known element ID or end of string.
    return re.compile(
        rf"({id_pattern})" +        # group 1: element ID
        r"(.+?)" +                   # group 2: value (lazy, but safe because lookahead is anchored)
        rf"(?={id_pattern}|\Z)",     # lookahead: next known ID or EOS
        re.DOTALL,
    )


_FIELD_RE: re.Pattern = _build_field_re()


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
        Values are stripped of ALL control characters (\r, \n, \x00, \x1e, \x1d).

    normalized_fields : Dict[str, str]
        Same as raw_fields but with date values converted to ISO 8601
        (YYYY-MM-DD). Non-date fields are unchanged.

    jurisdiction_fields : Dict[str, str]
        All Z-prefixed state-specific fields (ZAA, ZCA, ZVA, ...).
        These are also present in raw_fields / normalized_fields.

    parse_method : str
        Which parser produced this result: "library" | "fallback_a" | "fallback_b"
    """
    raw_fields: Dict[str, str] = field(default_factory=dict)
    normalized_fields: Dict[str, str] = field(default_factory=dict)
    jurisdiction_fields: Dict[str, str] = field(default_factory=dict)
    parse_method: str = "fallback_a"


# ---------------------------------------------------------------------------
# Value cleaning
# ---------------------------------------------------------------------------

def _clean_value(value: str) -> str:
    """
    Strip ALL AAMVA delimiter control characters from a field value and
    trim surrounding whitespace.

    The AAMVA PDF417 format uses \n (0x0A) as the field delimiter,
    \x1e (0x1E) / \x1d (0x1D) as record/subfile separators, and \r
    (0x0D) as a carriage return. Barcode decoders embed these characters
    in the decoded string.

    If they are not stripped from each stored value, every downstream
    check fails:

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
    and must be preserved so that _fallback_parse Strategy A can split
    on them. Only NUL bytes (\x00) are stripped globally.
    """
    cleaned = raw.replace("\x00", "")
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
    Input is cleaned of control chars and stripped before parsing.
    """
    raw = _clean_value(mmddccyy)
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
    ISO 8601. Non-date fields are re-cleaned as a safety net.
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
    """Pull all Z-prefixed element IDs into a separate dict."""
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
                # CRITICAL: always clean values from the library path too.
                # The library does not guarantee control-char-free output.
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

    Handles all four delimiter variants used by different PDF417
    decoders and card-writer implementations:
      zxingcpp on AAMVA 2010+  : \n
      pyzbar on some encoders  : \r\n
      Some older card writers  : \x1e (ASCII Record Separator)
      Some encoders            : \x1d (ASCII Group Separator)
    """
    return re.split(r"[\r\n\x1e\x1d]+", data_section)


# ---------------------------------------------------------------------------
# DL subfile header parser
# ---------------------------------------------------------------------------

def _extract_from_header_line(line: str) -> Optional[tuple[str, str]]:
    """
    Extract the first element ID and value from a DL/ID subfile header line.

    The AAMVA DL subfile header has the format:
        DL<RecordLength><ElementId><Value>
    e.g.: DL0280DAQ12345678
          DL028000280DAQ12345678  (some encoders repeat length)

    The record length is a variable number of digits. This function
    scans forward past any leading digits after the DL/ID prefix to
    find the first valid 3-char element ID.

    Returns (elem_id, value) tuple or None if no element ID found.
    """
    # Strip the DL/ID prefix
    m = re.match(r'^(DL|ID)', line)
    if not m:
        return None
    remainder = line[m.end():]

    # Skip all leading digit characters (record length field)
    remainder = remainder.lstrip('0123456789')

    # Now look for a valid 3-char element ID
    if len(remainder) >= 4 and re.match(r'^[A-Z]{2}[A-Z0-9]', remainder):
        elem_id = remainder[:3]
        value = _clean_value(remainder[3:])
        if value:
            return (elem_id, value)

    return None


# ---------------------------------------------------------------------------
# Fallback hand-rolled parser
# ---------------------------------------------------------------------------

def _fallback_parse(raw: str) -> tuple[Dict[str, str], str]:
    """
    Hand-rolled AAMVA parser. Returns (fields_dict, method_used).

    Strategy A: line-by-line split on AAMVA delimiters [\r\n\x1e\x1d].
    Strategy B: compact regex scan using a known-ID-anchored pattern.
                Only runs if Strategy A yields < 5 fields.

    KEY FIX: values are passed through _clean_value() when stored,
    not just .strip(), to guarantee all control characters are removed
    from every field value regardless of encoder behaviour.
    """
    fields: Dict[str, str] = {}

    # Locate DL subfile marker; fall back to start of string
    dl_marker = raw.find("DL")
    data_section = raw[dl_marker:] if dl_marker != -1 else raw

    # --- Strategy A: line-by-line ---
    lines = _split_field_section(data_section)

    for raw_line in lines:
        # Strip control characters from the entire line first
        line = _clean_value(raw_line)
        if not line:
            continue

        # Handle DL/ID subfile header line
        if re.match(r'^(DL|ID)', line):
            result = _extract_from_header_line(line)
            if result:
                elem_id, value = result
                if elem_id and value:
                    fields[elem_id] = value
            continue

        # Normal field line: must start with a 3-char element ID
        if len(line) >= 4 and re.match(r'^[A-Z]{2}[A-Z0-9]', line):
            elem_id = line[:3]
            # KEY FIX: use _clean_value on the value portion to strip any
            # trailing control characters that were not caught by the line
            # splitter (e.g. when encoder embeds \n inside a value).
            value = _clean_value(line[3:])
            if elem_id and value:
                fields[elem_id] = value

    if len(fields) >= 5:
        return fields, "fallback_a"

    # --- Strategy B: compact known-ID-anchored regex scan ---
    # Only reached when line splitting produced very few fields.
    # Uses _FIELD_RE built from known element IDs to prevent false splits.
    fields.clear()
    compact = data_section.translate(_CTRL_CHARS)

    for m in _FIELD_RE.finditer(compact):
        elem_id = m.group(1)
        # group 2 index depends on how many alternatives are in the ID group.
        # Since we have a flat alternation in group 1, group 2 is the value.
        value = _clean_value(m.group(2))
        if value:
            fields[elem_id] = value

    return fields, "fallback_b"


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
        .raw_fields          -- all element IDs, control-char-free values
        .normalized_fields   -- same but date fields in YYYY-MM-DD
        .jurisdiction_fields -- Z-prefixed state-specific fields
        .parse_method        -- "library" | "fallback_a" | "fallback_b"

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

    # --- Strategy 2 & 3: fallback (A then B) ---
    if not raw_fields or len(raw_fields) < 5:
        raw_fields, method = _fallback_parse(raw_barcode)

    if len(raw_fields) < 3:
        raise ValueError(
            f"AAMVA parser extracted only {len(raw_fields)} fields -- "
            "barcode may be malformed or non-AAMVA"
        )

    # --- Post-processing ---
    # _apply_date_normalization re-cleans all values as a safety net
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
