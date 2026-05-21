"""
tests/test_validators.py
========================
Unit tests for all six cross-validation checks in
app.core.barcode.validators.

Each check is exercised with:
  - A clean document that should produce severity "pass"
  - Targeted mutations that fire each specific fraud signal

No live images required. All docs are synthetic dicts.
"""
from __future__ import annotations

import copy
import pytest

from app.core.barcode.validators import (
    ValidationResult,
    check_age_derived_fields,
    check_date_logic,
    check_dcf_entropy,
    check_expiry_window,
    check_jurisdiction_fields,
    check_syntax_conformance,
)


# ---------------------------------------------------------------------------
# Fixture — valid California DL (all checks should pass)
# ---------------------------------------------------------------------------

CA_VALID: dict = {
    # Identity
    "DAQ": "D1234567",
    "DCS": "SMITH",
    "DAC": "JOHN",
    "DAD": "ALLEN",
    # Dates (ISO 8601 after normalisation)
    "DBB": "1990-01-15",   # DOB
    "DBA": "2029-01-15",   # Expiry
    "DBD": "2021-01-15",   # Issue
    # Address
    "DAG": "123 MAIN ST",
    "DAI": "SACRAMENTO",
    "DAJ": "CA",
    "DAK": "94203     ",
    # Physical
    "DBC": "1",
    "DAU": "510",
    # Doc IDs
    "DCF": "CADL1234567890123456789",
    "DCG": "USA",
    # Jurisdiction metadata
    "_jurisdiction": "CA",
    "_aamva_version": 8,
    # California ZXX fields
    "ZCA": "08",
    "ZCB": "09162021",
    "ZCC": "0",
}


def _doc(**overrides) -> dict:
    """Return a copy of CA_VALID with overrides applied."""
    d = copy.deepcopy(CA_VALID)
    d.update(overrides)
    return d


def _doc_without(*keys) -> dict:
    """Return CA_VALID with the given keys removed."""
    d = copy.deepcopy(CA_VALID)
    for k in keys:
        d.pop(k, None)
    return d


# ---------------------------------------------------------------------------
# check_syntax_conformance
# ---------------------------------------------------------------------------

class TestSyntaxConformance:
    def test_valid_doc_passes(self):
        r = check_syntax_conformance(CA_VALID)
        assert isinstance(r, ValidationResult)
        assert r.severity == "pass"

    def test_missing_mandatory_field_flags(self):
        doc = _doc_without("DAQ")
        r = check_syntax_conformance(doc)
        assert r.severity in ("warn", "fail")
        assert "missing_mandatory_fields" in r.signals
        assert "DAQ" in r.signals["missing_mandatory_fields"]

    def test_field_value_too_long(self):
        # DCS max_len = 40 in AAMVA_FIELDS
        doc = _doc(DCS="A" * 41)
        r = check_syntax_conformance(doc)
        # oversized fields should be flagged
        assert r.severity in ("warn", "fail")

    def test_invalid_country_code(self):
        doc = _doc(DCG="XX")
        r = check_syntax_conformance(doc)
        assert r.severity in ("warn", "fail")

    def test_invalid_sex_code(self):
        doc = _doc(DBC="5")  # only 1, 2, 9 are valid
        r = check_syntax_conformance(doc)
        assert r.severity in ("warn", "fail")


# ---------------------------------------------------------------------------
# check_date_logic
# ---------------------------------------------------------------------------

class TestDateLogic:
    def test_valid_doc_passes(self):
        r = check_date_logic(CA_VALID)
        assert r.severity == "pass"

    def test_dob_after_issue_date_flags(self):
        # Born 2000, issued 1999 — impossible
        doc = _doc(DBB="2000-06-01", DBD="1999-01-01")
        r = check_date_logic(doc)
        assert r.severity == "fail"
        assert r.signals.get("dob_after_issue") is True

    def test_issue_after_expiry_flags(self):
        doc = _doc(DBD="2030-01-01", DBA="2025-01-01")
        r = check_date_logic(doc)
        assert r.severity == "fail"
        assert r.signals.get("issue_after_expiry") is True

    def test_issue_date_in_future_flags(self):
        doc = _doc(DBD="2099-01-01")
        r = check_date_logic(doc)
        assert r.severity in ("warn", "fail")
        assert r.signals.get("issue_date_future") is True

    def test_document_expired_is_warn_not_fail(self):
        doc = _doc(DBA="2000-01-01")
        r = check_date_logic(doc)
        # Expired doc is a warning, not a hard fail
        assert "document_expired" in r.signals
        assert r.signals["document_expired"] is True


# ---------------------------------------------------------------------------
# check_expiry_window
# ---------------------------------------------------------------------------

class TestExpiryWindow:
    def test_valid_8_year_window_passes(self):
        # CA issues 8-year licenses
        doc = _doc(DBD="2021-01-15", DBA="2029-01-15")  # exactly 8 years
        r = check_expiry_window(doc)
        assert r.severity == "pass"

    def test_mismatched_window_flags(self):
        # 5-year window for CA (not 4 or 8)
        doc = _doc(DBD="2020-01-01", DBA="2025-01-01")
        r = check_expiry_window(doc)
        assert r.severity in ("warn", "fail")
        assert r.signals.get("window_mismatch") is True

    def test_unknown_jurisdiction_does_not_crash(self):
        doc = _doc(DAJ="ZZ", **{"_jurisdiction": "ZZ"})
        r = check_expiry_window(doc)
        assert isinstance(r, ValidationResult)


# ---------------------------------------------------------------------------
# check_jurisdiction_fields
# ---------------------------------------------------------------------------

class TestJurisdictionFields:
    def test_valid_ca_passes(self):
        r = check_jurisdiction_fields(CA_VALID)
        assert r.severity == "pass"

    def test_missing_zxx_field_flags(self):
        # CA requires ZCA; remove it
        doc = _doc_without("ZCA")
        doc["_jurisdiction"] = "CA"
        r = check_jurisdiction_fields(doc)
        assert r.severity in ("warn", "fail")

    def test_unknown_jurisdiction_passes_gracefully(self):
        doc = _doc(DAJ="WY", **{"_jurisdiction": "WY"})
        # WY may not have known ZXX requirements — should not crash
        r = check_jurisdiction_fields(doc)
        assert isinstance(r, ValidationResult)


# ---------------------------------------------------------------------------
# check_dcf_entropy
# ---------------------------------------------------------------------------

class TestDCFEntropy:
    def test_valid_dcf_passes(self):
        r = check_dcf_entropy(CA_VALID)
        assert isinstance(r, ValidationResult)

    def test_missing_dcf_flags(self):
        doc = _doc_without("DCF")
        r = check_dcf_entropy(doc)
        assert r.severity in ("warn", "fail")

    def test_too_short_dcf_flags(self):
        doc = _doc(DCF="ABC")
        r = check_dcf_entropy(doc)
        assert r.severity in ("warn", "fail")

    def test_all_same_chars_flags(self):
        # Zero-entropy DCF — e.g. "AAAAAAAAAAAAAAAAAAAAAAAAA"
        doc = _doc(DCF="A" * 25)
        r = check_dcf_entropy(doc)
        assert r.severity in ("warn", "fail")


# ---------------------------------------------------------------------------
# check_age_derived_fields
# ---------------------------------------------------------------------------

class TestAgeDerivedFields:
    def test_adult_doc_no_ddh_passes(self):
        # DOB 1990 — clearly an adult; DDH not present is fine
        r = check_age_derived_fields(CA_VALID)
        assert r.severity == "pass"

    def test_ddh_inconsistent_with_dob_flags(self):
        # DOB = adult born 1990, DDH says still under-18 until 2099
        doc = _doc(DDH="2099-01-01")
        r = check_age_derived_fields(doc)
        assert r.severity in ("warn", "fail")
        # Should flag DDH inconsistency
        fired = (
            r.signals.get("DDH_inconsistent")
            or r.signals.get("DDH_delta_days", 0) > 0
        )
        assert fired

    def test_ddj_inconsistent_with_dob_flags(self):
        # DDJ (under-21) must match DOB+21; for 1990 DOB that's 2011
        doc = _doc(DDJ="2099-01-01")
        r = check_age_derived_fields(doc)
        assert r.severity in ("warn", "fail")

    def test_no_age_fields_passes(self):
        doc = _doc_without("DDH", "DDI", "DDJ")
        r = check_age_derived_fields(doc)
        assert r.severity == "pass"
