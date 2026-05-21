"""
AAMVA Cross-Validation Engine
==============================
Six independently testable validator functions that inspect a
ParsedAAMVADocument and return structured ValidationResult objects.

Public API
----------
    check_syntax_conformance(doc)  -> ValidationResult
    check_date_logic(doc)          -> ValidationResult
    check_expiry_window(doc)       -> ValidationResult
    check_jurisdiction_fields(doc) -> ValidationResult
    check_dcf_entropy(doc)         -> ValidationResult
    check_age_derived_fields(doc)  -> ValidationResult

Each function is pure (no I/O, no global state mutation) and can be
called in parallel or independently in unit tests.

ValidationResult
----------------
    passed   : bool   -- True iff no hard failures were found
    severity : str    -- "pass" | "warn" | "fail"
    check    : str    -- name of the check that produced this result
    details  : list   -- list of human-readable finding strings
    signals  : dict   -- machine-readable k/v pairs for risk scorer

IMPORTANT: All field value comparisons use _cv(value) (an alias for
_clean_value from parser.py) before any length / regex / enum check.
This guarantees correctness even if an upstream parser path (e.g.
the aamva-barcode-library) returns values that still contain trailing
control characters such as \n or \x1e.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from app.core.barcode.jurisdiction_config import (
    DCF_MIN_ENTROPY_BITS,
    DCF_PATTERNS,
    EXPIRY_TOLERANCE_YEARS,
    EXPIRY_WINDOWS,
    JURISDICTION_ZXX_RULES,
)
from app.core.barcode.parser import AAMVA_FIELDS, ParsedAAMVADocument, _clean_value
from app.utils.logger import logger

# Alias for brevity -- every value comparison goes through this.
_cv = _clean_value


# ---------------------------------------------------------------------------
# Shared result type
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    """
    Result of a single cross-validation check.

    Attributes
    ----------
    check    : identifier matching the function name
    passed   : True iff severity is "pass"
    severity : "pass" | "warn" | "fail"
    details  : human-readable list of findings (empty on clean pass)
    signals  : machine-readable dict for downstream risk scorer
    """
    check: str
    passed: bool
    severity: str  # "pass" | "warn" | "fail"
    details: List[str] = field(default_factory=list)
    signals: Dict[str, object] = field(default_factory=dict)


def _result(check: str, details: List[str], signals: Dict) -> ValidationResult:
    """Build a ValidationResult; severity derived from non-empty details list."""
    has_fail = any(d.startswith("[FAIL]") for d in details)
    has_warn = any(d.startswith("[WARN]") for d in details)
    if has_fail:
        severity = "fail"
    elif has_warn:
        severity = "warn"
    else:
        severity = "pass"
    return ValidationResult(
        check=check,
        passed=(severity == "pass"),
        severity=severity,
        details=details,
        signals=signals,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_iso_date(value: str, field_id: str) -> Optional[date]:
    """Parse YYYY-MM-DD string to date. Returns None if unparseable."""
    if not value:
        return None
    try:
        return datetime.strptime(_cv(value).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _shannon_entropy(s: str) -> float:
    """
    Compute Shannon entropy in bits for a string.
    Used to detect trivially low-entropy DCF values.
    """
    if not s:
        return 0.0
    counts = Counter(s)
    length = len(s)
    return -sum(
        (c / length) * math.log2(c / length)
        for c in counts.values()
        if c > 0
    )


def _severity(details: List[str]) -> str:
    if any(d.startswith("[FAIL]") for d in details):
        return "fail"
    if any(d.startswith("[WARN]") for d in details):
        return "warn"
    return "pass"


# ---------------------------------------------------------------------------
# 1. Syntax conformance
# ---------------------------------------------------------------------------

def check_syntax_conformance(doc: ParsedAAMVADocument) -> ValidationResult:
    """
    Validate that each present field satisfies AAMVA structural constraints:
      - Value does not exceed max_len defined in AAMVA_FIELDS
      - Date fields are exactly 8 digits before normalization
      - DBC (Sex) is one of {"1", "2", "9"}
      - DAJ (Jurisdiction code) is exactly 2 uppercase letters
      - DCG (Country) is one of {"USA", "CAN"}
      - DAK (Postal code) is 5 or 9 digits (US) or 6 alphanumeric (CA)
      - All mandatory fields are present

    FIX: All value comparisons now use _cv(value) to strip control
    characters before any length/regex/enum check. Previously .strip()
    was used which does not remove \n or \x1e, causing false positives:
      'NC\n'       -> len 3, fails max_len=2 for DAJ
      '03031974\n' -> fails re.fullmatch(r'\d{8}', ...)
      '1\n'        -> not in {"1","2","9"} for DBC
      'USA\n'      -> flagged as dcg_unexpected
    """
    CHECK = "check_syntax_conformance"
    details: List[str] = []
    signals: Dict = {}

    raw = doc.raw_fields

    # --- Mandatory field presence ---
    missing_mandatory = [
        fid for fid, meta in AAMVA_FIELDS.items()
        if meta.get("required") and fid not in raw
    ]
    if missing_mandatory:
        for fid in missing_mandatory:
            details.append(f"[FAIL] Missing mandatory field: {fid} ({AAMVA_FIELDS[fid]['name']})")
        signals["missing_mandatory_fields"] = missing_mandatory

    # --- Field-level constraints ---
    for fid, value in raw.items():
        meta = AAMVA_FIELDS.get(fid)
        # KEY FIX: use _cv() to strip ALL control characters, not just spaces
        clean_value = _cv(value)

        # Max length check (applies to all known fields)
        if meta and len(clean_value) > meta["max_len"]:
            details.append(
                f"[FAIL] {fid} value length {len(clean_value)} exceeds max {meta['max_len']}: "
                f"'{clean_value[:30]}'"
            )
            signals[f"{fid}_length_violation"] = len(clean_value)

    # --- Specific field format checks ---
    # All values are cleaned with _cv() before comparison.

    # Sex code
    if "DBC" in raw:
        dbc = _cv(raw["DBC"])
        if dbc not in {"1", "2", "9"}:
            details.append(f"[FAIL] DBC (Sex) has invalid value: '{dbc}' (expected 1, 2, or 9)")
            signals["dbc_invalid"] = dbc

    # Jurisdiction code: exactly 2 uppercase letters
    if "DAJ" in raw:
        daj = _cv(raw["DAJ"])
        if not re.fullmatch(r"[A-Z]{2}", daj):
            details.append(f"[FAIL] DAJ (Jurisdiction) invalid format: '{daj}'")
            signals["daj_invalid"] = daj

    # Country: USA or CAN
    if "DCG" in raw:
        dcg = _cv(raw["DCG"]).upper()
        if dcg not in {"USA", "CAN"}:
            details.append(f"[WARN] DCG (Country) unexpected value: '{dcg}'")
            signals["dcg_unexpected"] = dcg

    # Postal code: US = 5 digits or 9 digits (ZIP+4 concatenated); CA = 6 alphanumeric
    if "DAK" in raw:
        dak_raw = _cv(raw["DAK"])
        dak = dak_raw.replace("-", "").replace(" ", "")
        country = _cv(raw.get("DCG", "USA")).upper()
        if country == "USA" and not re.fullmatch(r"\d{5,9}", dak):
            details.append(f"[WARN] DAK (Postal Code) unexpected US format: '{dak_raw}'")
            signals["dak_format_warn"] = dak_raw
        elif country == "CAN" and not re.fullmatch(r"[A-Z0-9]{6}", dak.upper()):
            details.append(f"[WARN] DAK (Postal Code) unexpected CA format: '{dak_raw}'")
            signals["dak_format_warn"] = dak_raw

    # Date fields: raw value must be exactly 8 digits (MMDDCCYY)
    date_field_ids = {"DBB", "DBA", "DBD", "DDH", "DDI", "DDJ"}
    for fid in date_field_ids:
        if fid in raw:
            # KEY FIX: clean the raw date value before regex check
            raw_date = _cv(raw[fid])
            if not re.fullmatch(r"\d{8}", raw_date):
                details.append(
                    f"[FAIL] {fid} date value is not 8 digits: '{raw_date}'"
                )
                signals[f"{fid}_format_invalid"] = raw_date

    signals["mandatory_fields_present"] = len(missing_mandatory) == 0
    signals["total_fields_found"] = len(raw)

    logger.info(CHECK, severity=_severity(details), field_count=len(raw))
    return _result(CHECK, details, signals)


# ---------------------------------------------------------------------------
# 2. Date logic
# ---------------------------------------------------------------------------

def check_date_logic(doc: ParsedAAMVADocument) -> ValidationResult:
    """
    Validate date fields using normalized (ISO 8601) values from doc.normalized_fields.

    Checks:
      - DBB, DBA, DBD are parseable as valid calendar dates
      - DBD (issue) < DBA (expiry)
      - DBB (DOB) < DBD (issue)
      - DBA must not already be expired
      - DBD must not be in the future
      - DOB implies a plausible age at time of issue (15-120 years)
    """
    CHECK = "check_date_logic"
    details: List[str] = []
    signals: Dict = {}
    norm = doc.normalized_fields
    today = date.today()

    parse_results: Dict[str, Optional[date]] = {}

    def get_date(fid: str) -> Optional[date]:
        if fid not in norm:
            parse_results[fid] = None
            return None
        # KEY FIX: clean the normalized value before parsing
        d = _parse_iso_date(_cv(norm[fid]), fid)
        parse_results[fid] = d
        if d is None:
            details.append(
                f"[FAIL] {fid} cannot be parsed as a valid date: '{norm[fid]}' "
                f"(raw: '{doc.raw_fields.get(fid, 'missing')}')"
            )
            signals[f"{fid}_parse_fail"] = _cv(norm[fid])
        return d

    dob = get_date("DBB")
    issue = get_date("DBD")
    expiry = get_date("DBA")

    required_date_fields = ["DBB", "DBD", "DBA"]
    dates_parseable = all(
        parse_results.get(fid) is not None
        for fid in required_date_fields
        if fid in norm
    )
    signals["dates_parseable"] = dates_parseable

    if dob and issue and expiry:
        if not (dob < issue):
            details.append(
                f"[FAIL] DBB (DOB={dob}) is not before DBD (Issue={issue}) -- "
                "impossible chronology"
            )
            signals["dob_after_issue"] = True

        if not (issue < expiry):
            details.append(
                f"[FAIL] DBD (Issue={issue}) is not before DBA (Expiry={expiry})"
            )
            signals["issue_after_expiry"] = True

        if expiry < today:
            details.append(
                f"[WARN] DBA (Expiry={expiry}) is in the past -- document is expired"
            )
            signals["document_expired"] = True
        else:
            signals["document_expired"] = False

        if issue > today:
            details.append(
                f"[FAIL] DBD (Issue={issue}) is in the future -- fabricated issue date"
            )
            signals["issue_date_future"] = True

        age_at_issue = (issue - dob).days / 365.25
        if not (15 <= age_at_issue <= 120):
            details.append(
                f"[FAIL] DBB implies age at issue of {age_at_issue:.1f} years -- "
                "outside plausible range (15-120)"
            )
            signals["age_at_issue_implausible"] = round(age_at_issue, 1)

    logger.info(CHECK, severity=_severity(details))
    return _result(CHECK, details, signals)


# ---------------------------------------------------------------------------
# 3. Expiry window
# ---------------------------------------------------------------------------

def check_expiry_window(doc: ParsedAAMVADocument) -> ValidationResult:
    """
    Verify that the issue-to-expiry span matches the issuing state's known policy.
    """
    CHECK = "check_expiry_window"
    details: List[str] = []
    signals: Dict = {}
    norm = doc.normalized_fields

    # KEY FIX: use _cv() when reading DAJ to strip any trailing \n
    jurisdiction = _cv(doc.raw_fields.get("DAJ", "")).upper()
    signals["jurisdiction"] = jurisdiction

    if not jurisdiction:
        details.append("[WARN] DAJ (Jurisdiction) missing -- expiry window check skipped")
        return _result(CHECK, details, signals)

    known_windows = EXPIRY_WINDOWS.get(jurisdiction)
    if known_windows is None:
        details.append(
            f"[WARN] Jurisdiction '{jurisdiction}' not in EXPIRY_WINDOWS config -- "
            "window check skipped"
        )
        signals["jurisdiction_unknown"] = True
        return _result(CHECK, details, signals)

    issue = _parse_iso_date(_cv(norm.get("DBD", "")), "DBD")
    expiry = _parse_iso_date(_cv(norm.get("DBA", "")), "DBA")

    if not issue or not expiry:
        details.append("[WARN] Cannot check expiry window -- DBD or DBA failed to parse")
        return _result(CHECK, details, signals)

    span_years = (expiry - issue).days / 365.25
    signals["span_years"] = round(span_years, 2)
    signals["expected_windows"] = known_windows

    tol = EXPIRY_TOLERANCE_YEARS
    matched = any(
        abs(span_years - expected) <= tol
        for expected in known_windows
    )

    if not matched:
        details.append(
            f"[FAIL] {jurisdiction} issue-to-expiry span is {span_years:.2f} years; "
            f"expected one of {known_windows} (±{tol} year tolerance)"
        )
        signals["window_mismatch"] = True
    else:
        signals["window_mismatch"] = False

    logger.info(CHECK, jurisdiction=jurisdiction, span_years=round(span_years, 2),
                matched=matched)
    return _result(CHECK, details, signals)


# ---------------------------------------------------------------------------
# 4. Jurisdiction fields (ZXX)
# ---------------------------------------------------------------------------

def check_jurisdiction_fields(doc: ParsedAAMVADocument) -> ValidationResult:
    """
    For states with known ZXX field rules (JURISDICTION_ZXX_RULES),
    verify:
      - All required Z-prefixed fields are present
      - Field values match the expected regex pattern where defined
    """
    CHECK = "check_jurisdiction_fields"
    details: List[str] = []
    signals: Dict = {}

    # KEY FIX: clean DAJ before lookup
    jurisdiction = _cv(doc.raw_fields.get("DAJ", "")).upper()
    signals["jurisdiction"] = jurisdiction
    signals["zxx_fields_found"] = list(doc.jurisdiction_fields.keys())

    rules = JURISDICTION_ZXX_RULES.get(jurisdiction)
    if rules is None:
        if doc.jurisdiction_fields:
            details.append(
                f"[WARN] No ZXX validation rules on file for '{jurisdiction}'; "
                f"found fields: {list(doc.jurisdiction_fields.keys())}"
            )
        signals["rules_available"] = False
        return _result(CHECK, details, signals)

    signals["rules_available"] = True

    for required_fid in rules.get("required", []):
        if required_fid not in doc.jurisdiction_fields:
            details.append(
                f"[FAIL] Required ZXX field {required_fid} missing for "
                f"jurisdiction '{jurisdiction}'"
            )
            signals[f"{required_fid}_missing"] = True

    for fid, pattern in rules.get("patterns", {}).items():
        if fid in doc.jurisdiction_fields:
            value = _cv(doc.jurisdiction_fields[fid])
            if not re.fullmatch(pattern, value):
                details.append(
                    f"[FAIL] {fid} value '{value}' does not match expected "
                    f"pattern for '{jurisdiction}': {pattern}"
                )
                signals[f"{fid}_pattern_fail"] = value

    logger.info(CHECK, jurisdiction=jurisdiction,
                zxx_count=len(doc.jurisdiction_fields))
    return _result(CHECK, details, signals)


# ---------------------------------------------------------------------------
# 5. DCF entropy & pattern
# ---------------------------------------------------------------------------

def check_dcf_entropy(doc: ParsedAAMVADocument) -> ValidationResult:
    """
    Document Discriminator (DCF) validation.

    Stage 1: pattern match (if jurisdiction has a known pattern in DCF_PATTERNS)
    Stage 2: entropy fallback (Shannon entropy < DCF_MIN_ENTROPY_BITS is suspicious)
    """
    CHECK = "check_dcf_entropy"
    details: List[str] = []
    signals: Dict = {}

    # KEY FIX: clean both values before use
    dcf = _cv(doc.raw_fields.get("DCF", ""))
    jurisdiction = _cv(doc.raw_fields.get("DAJ", "")).upper()

    if not dcf:
        details.append("[WARN] DCF (Document Discriminator) field is missing")
        signals["dcf_missing"] = True
        return _result(CHECK, details, signals)

    signals["dcf_length"] = len(dcf)
    signals["jurisdiction"] = jurisdiction

    pattern = DCF_PATTERNS.get(jurisdiction)
    if pattern is not None:
        if re.fullmatch(pattern, dcf):
            signals["dcf_pattern_match"] = True
            logger.info(CHECK, stage="pattern_match", jurisdiction=jurisdiction, passed=True)
            return _result(CHECK, details, signals)
        else:
            details.append(
                f"[FAIL] DCF '{dcf}' does not match known pattern for "
                f"'{jurisdiction}': {pattern}"
            )
            signals["dcf_pattern_match"] = False
            logger.info(CHECK, stage="pattern_match", jurisdiction=jurisdiction, passed=False)
            return _result(CHECK, details, signals)

    entropy = _shannon_entropy(dcf)
    signals["dcf_entropy_bits"] = round(entropy, 3)
    signals["dcf_pattern_match"] = None

    if entropy < DCF_MIN_ENTROPY_BITS:
        details.append(
            f"[WARN] DCF entropy {entropy:.3f} bits is below threshold "
            f"{DCF_MIN_ENTROPY_BITS} -- possible fabricated value: '{dcf}'"
        )
        signals["dcf_low_entropy"] = True
    else:
        signals["dcf_low_entropy"] = False

    logger.info(CHECK, stage="entropy", jurisdiction=jurisdiction, entropy=round(entropy, 3))
    return _result(CHECK, details, signals)


# ---------------------------------------------------------------------------
# 6. Age-derived field consistency
# ---------------------------------------------------------------------------

def check_age_derived_fields(doc: ParsedAAMVADocument) -> ValidationResult:
    """
    Validate logical consistency between DBB (Date of Birth) and the
    age-gating fields DDH / DDI / DDJ.

    AAMVA definitions:
      DDH : date holder turns 18  (DBB + 18 years)
      DDI : date holder turns 19  (DBB + 19 years)
      DDJ : date holder turns 21  (DBB + 21 years)

    Tolerance: ±2 days for leap-year arithmetic differences.
    """
    CHECK = "check_age_derived_fields"
    details: List[str] = []
    signals: Dict = {}
    norm = doc.normalized_fields

    dob = _parse_iso_date(_cv(norm.get("DBB", "")), "DBB")
    if not dob:
        details.append("[WARN] DBB (Date of Birth) missing or unparseable -- age-derived check skipped")
        return _result(CHECK, details, signals)

    TOLERANCE_DAYS = 2
    AGE_FIELDS = {"DDH": 18, "DDI": 19, "DDJ": 21}

    for fid, years in AGE_FIELDS.items():
        if fid not in norm:
            continue

        declared = _parse_iso_date(_cv(norm[fid]), fid)
        if not declared:
            details.append(
                f"[FAIL] {fid} (Under-{years} Until) present but unparseable: '{norm[fid]}'"
            )
            signals[f"{fid}_parse_fail"] = norm[fid]
            continue

        expected_days = int(years * 365.25)
        expected = dob + timedelta(days=expected_days)
        delta_days = abs((declared - expected).days)
        signals[f"{fid}_delta_days"] = delta_days

        if delta_days > TOLERANCE_DAYS:
            details.append(
                f"[FAIL] {fid} (Under-{years} Until) declared as {declared} but "
                f"expected ~{expected} given DBB={dob} -- delta {delta_days} days "
                f"(tolerance ±{TOLERANCE_DAYS} days)"
            )
            signals[f"{fid}_inconsistent"] = True
        else:
            signals[f"{fid}_inconsistent"] = False

    today = date.today()
    current_age = (today - dob).days / 365.25
    if "DDJ" in norm and current_age >= 21:
        details.append(
            f"[WARN] DDJ (Under-21 Until) present but holder current age is "
            f"{current_age:.1f} years -- field should not appear on renewed license"
        )
        signals["ddj_stale"] = True

    logger.info(CHECK, dob=str(dob), age_fields_checked=[
        k for k in AGE_FIELDS if k in norm
    ])
    return _result(CHECK, details, signals)
