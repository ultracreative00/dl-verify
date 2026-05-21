"""
Risk Scorer Configuration
==========================
All weights, thresholds, and hard-override rules live here.
Change this file to retune the scorer without touching logic.

Design principles
-----------------
1. WEIGHTS  — per-check contribution to the weighted fraud score.
   Each check maps to a float 0.0–1.0.  Weights do NOT need to sum
   to 1.0; the scorer normalises them internally.

2. SIGNAL_WEIGHTS  — fine-grained per-signal overrides inside each
   check bucket.  When a check emits multiple signals, these weights
   control which ones punch harder into the bucket score.

3. THRESHOLDS  — score bands that produce the final recommendation:
     [0.0, PASS_MAX]       -> PASS
     (PASS_MAX, REVIEW_MAX]-> REVIEW
     (REVIEW_MAX, 1.0]     -> REJECT

4. HARD_FAILS  — signal keys whose mere presence (truthy value)
   forces REJECT regardless of the numeric score.  Reserved for
   logically-impossible conditions (impossible date, barcode unreadable).

5. HARD_WARNS  — signal keys that force at least REVIEW (floor lift).
"""
from __future__ import annotations
from typing import Dict, List

# ---------------------------------------------------------------------------
# Check-level weights
# Relative importance of each ValidationResult in the aggregated score.
# Higher = more influence.  Sum is normalised by the scorer.
# ---------------------------------------------------------------------------
CHECK_WEIGHTS: Dict[str, float] = {
    "check_syntax_conformance":  1.0,   # Hard structural violations — highest weight
    "check_date_logic":          1.0,   # Impossible dates = automatic fraud signals
    "check_expiry_window":       0.75,  # State policy mismatch — strong but not absolute
    "check_jurisdiction_fields": 0.70,  # ZXX field fingerprint
    "check_dcf_entropy":         0.65,  # DCF pattern / entropy — probabilistic
    "check_age_derived_fields":  0.60,  # DDH/DDI/DDJ consistency
    # Sprint 2+ checks (registered here so scorer is forward-compatible)
    "check_ocr_barcode_diff":    0.90,  # Front OCR <-> barcode mismatch — very high
    "check_image_quality":       0.40,  # Quality gate — lower weight, mostly a pre-filter
}

# ---------------------------------------------------------------------------
# Signal-level weights (within a check bucket)
# When multiple signals fire inside one check, these multipliers
# scale how much each individual signal contributes to that bucket's
# failure score.  Unregistered signals default to 0.5.
# ---------------------------------------------------------------------------
SIGNAL_WEIGHTS: Dict[str, float] = {
    # check_syntax_conformance
    "missing_mandatory_fields":   1.0,
    "dbc_invalid":                0.8,
    "daj_invalid":                0.7,
    "dcg_unexpected":             0.3,
    "dak_format_warn":            0.2,

    # check_date_logic
    "dob_after_issue":            1.0,
    "issue_after_expiry":         1.0,
    "issue_date_future":          1.0,
    "age_at_issue_implausible":   0.9,
    "document_expired":           0.1,   # Expired doc is low-signal fraud (user problem)

    # check_expiry_window
    "window_mismatch":            0.9,
    "jurisdiction_unknown":       0.1,

    # check_jurisdiction_fields
    "rules_available":            0.0,   # Meta-signal — not a fraud indicator
    "ZCA_missing":                0.8,
    "ZCB_missing":                0.6,
    "ZCC_missing":                0.6,
    "ZTZ_missing":                0.8,
    "ZFZ_missing":                0.8,
    "ZNY_missing":                0.8,
    "ZIL_missing":                0.7,
    "ZVA_missing":                0.7,

    # check_dcf_entropy
    "dcf_pattern_match":          0.0,   # True = pass (0 contribution), False handled below
    "dcf_missing":                0.6,
    "dcf_low_entropy":            0.5,

    # check_age_derived_fields
    "DDH_inconsistent":           0.85,
    "DDI_inconsistent":           0.80,
    "DDJ_inconsistent":           0.85,
    "ddj_stale":                  0.2,
}

# ---------------------------------------------------------------------------
# Score thresholds  (fraud probability 0.0 = clean, 1.0 = certain fraud)
# ---------------------------------------------------------------------------
PASS_MAX: float   = 0.20   # <= 0.20  -> PASS
REVIEW_MAX: float = 0.55   # <= 0.55  -> REVIEW
# > 0.55 -> REJECT

# ---------------------------------------------------------------------------
# Hard override rules
# ---------------------------------------------------------------------------

# Any truthy signal in this set forces recommendation to REJECT
# regardless of numeric score.
HARD_FAIL_SIGNALS: List[str] = [
    "dob_after_issue",
    "issue_after_expiry",
    "issue_date_future",
    "missing_mandatory_fields",   # list is truthy when non-empty
    "DDH_inconsistent",
    "DDJ_inconsistent",
    # Sprint 2+ signals
    "ocr_barcode_name_mismatch",
    "ocr_barcode_dob_mismatch",
    "ocr_barcode_dlnum_mismatch",
]

# Any truthy signal in this set lifts recommendation floor to at least REVIEW
HARD_WARN_SIGNALS: List[str] = [
    "document_expired",
    "window_mismatch",
    "dcf_pattern_match_false",    # sentinel set by scorer when dcf_pattern_match == False
    "dcf_missing",
    "dcf_low_entropy",
    "jurisdiction_unknown",
    "ddj_stale",
]
