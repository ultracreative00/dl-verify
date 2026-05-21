"""
Jurisdiction Policy Config
==========================
Static policy tables consumed by the cross-validation engine.
All data is sourced from publicly available DMV handbooks and the
AAMVA DL/ID Card Design Standard 2020/2025.

Structure
---------
EXPIRY_WINDOWS : Dict[str, List[int]]
    Mapping of 2-letter jurisdiction code -> list of valid
    issue-to-expiry spans in years.
    A tolerance of +/-1 year is applied by check_expiry_window().

DCF_PATTERNS : Dict[str, str]
    Mapping of jurisdiction code -> regex pattern for the
    Document Discriminator (DCF) field. None = skip pattern check.

JURISDICTION_ZXX_RULES : Dict[str, Dict]
    Per-state rules for Z-prefixed jurisdiction fields:
      required  : List[str]  — element IDs that MUST be present
      patterns  : Dict[str, str] — element_id -> regex for the value
"""
from __future__ import annotations
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Expiry window policy  (years from issue date to expiration date)
# Tolerance of ±1 year is applied in check_expiry_window()
# ---------------------------------------------------------------------------
EXPIRY_WINDOWS: Dict[str, List[int]] = {
    "AL": [4, 8],
    "AK": [5],
    "AZ": [5, 12],
    "AR": [4, 8],
    "CA": [5],
    "CO": [5],
    "CT": [6],
    "DE": [8],
    "DC": [8],
    "FL": [8],
    "GA": [8],
    "HI": [8],
    "ID": [4, 8],
    "IL": [4],
    "IN": [6],
    "IA": [8],
    "KS": [6],
    "KY": [4, 8],
    "LA": [4, 6],
    "ME": [6],
    "MD": [8],
    "MA": [5],
    "MI": [4],
    "MN": [4],
    "MS": [4, 8],
    "MO": [6],
    "MT": [4, 8],
    "NE": [5],
    "NV": [8],
    "NH": [5],
    "NJ": [4],
    "NM": [4, 6, 8],
    "NY": [8],
    "NC": [8],
    "ND": [4, 6],
    "OH": [4],
    "OK": [4],
    "OR": [8],
    "PA": [4],
    "RI": [5],
    "SC": [8, 10],
    "SD": [5],
    "TN": [8],
    "TX": [6],
    "UT": [5],
    "VT": [4],
    "VA": [8],
    "WA": [6],
    "WV": [5, 6],
    "WI": [8],
    "WY": [4],
    # Territories
    "GU": [4],
    "PR": [4],
    "VI": [4],
    "MP": [4],
    "AS": [4],
    # Canada
    "AB": [5],
    "BC": [5],
    "MB": [5],
    "NB": [4],
    "NL": [4],
    "NS": [5],
    "ON": [5],
    "PE": [4],
    "QC": [6],
    "SK": [5],
}

# Tolerance in years applied around each expected window value
EXPIRY_TOLERANCE_YEARS: int = 1


# ---------------------------------------------------------------------------
# DCF (Document Discriminator) known patterns per jurisdiction
# None = no validated pattern for this state (falls through to entropy check)
# ---------------------------------------------------------------------------
DCF_PATTERNS: Dict[str, Optional[str]] = {
    # California: 2 uppercase letters + up to 21 alphanumeric chars
    "CA": r"^[A-Z]{2}[A-Z0-9]{5,21}$",
    # Texas: all-numeric, 13-20 digits
    "TX": r"^\d{13,20}$",
    # Florida: starts with FL then alphanumeric
    "FL": r"^FL[A-Z0-9]{10,20}$",
    # New York: purely numeric, 10-15 digits
    "NY": r"^\d{10,15}$",
    # Illinois: alphanumeric, 12-18 chars
    "IL": r"^[A-Z0-9]{12,18}$",
    # Pennsylvania: all-numeric 10-14 digits
    "PA": r"^\d{10,14}$",
    # Ohio: alphanumeric, 12-16 chars
    "OH": r"^[A-Z0-9]{12,16}$",
    # Georgia: digits only, 9-15
    "GA": r"^\d{9,15}$",
    # Michigan: alphanumeric 11-15
    "MI": r"^[A-Z0-9]{11,15}$",
    # Washington: alphanumeric 12-18
    "WA": r"^[A-Z0-9]{12,18}$",
    # North Carolina: alphanumeric 10-20 chars
    # NC uses a mixed alphanumeric discriminator with no fixed prefix
    "NC": r"^[A-Z0-9]{10,20}$",
    # All others: None (skip pattern check, fall through to entropy check)
}

# Minimum Shannon entropy bits required for DCF when no pattern is known
DCF_MIN_ENTROPY_BITS: float = 2.5


# ---------------------------------------------------------------------------
# Jurisdiction ZXX field rules
# required : fields that must be present for a legitimate card
# patterns : per-field regex the value must satisfy
# ---------------------------------------------------------------------------
JURISDICTION_ZXX_RULES: Dict[str, Dict] = {
    "CA": {
        "required": ["ZCA", "ZCB", "ZCC"],
        "patterns": {
            "ZCA": r"^[A-Z0-9]{1,20}$",
        },
    },
    "TX": {
        "required": ["ZTZ"],
        "patterns": {
            "ZTZ": r"^[A-Z0-9]{1,20}$",
        },
    },
    "FL": {
        "required": ["ZFZ"],
        "patterns": {
            "ZFZ": r"^\d{2}[A-Z0-9]{0,18}$",
        },
    },
    "NY": {
        "required": ["ZNY"],
        "patterns": {
            "ZNY": r"^[A-Z0-9]{1,15}$",
        },
    },
    "IL": {
        "required": ["ZIL"],
        "patterns": {},
    },
    "VA": {
        "required": ["ZVA"],
        "patterns": {
            "ZVA": r"^[A-Z0-9]{1,20}$",
        },
    },
    # North Carolina: ZN0, ZNZ, ZNB, ZNC, ZND are the five standard
    # NC-specific fields confirmed present in NC barcode specimens.
    # ZN0 is the NC-specific document number suffix/check element.
    # Patterns are permissive alphanumeric — NC does not publish specs.
    "NC": {
        "required": ["ZN0"],
        "patterns": {
            "ZN0": r"^[A-Z0-9]{1,25}$",
            "ZNZ": r"^[A-Z0-9]{1,25}$",
            "ZNB": r"^[A-Z0-9]{1,25}$",
            "ZNC": r"^[A-Z0-9]{1,25}$",
            "ZND": r"^[A-Z0-9]{1,25}$",
        },
    },
    # States where ZXX fields are optional / unvalidated
    # (add entries as specimen data is collected)
}
