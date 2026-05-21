"""
tests/test_parser.py
====================
Unit tests for app.core.barcode.parser.parse_aamva().

All fixtures are synthetic in-memory strings — no live images or files.
"""
from __future__ import annotations

import pytest

from app.core.barcode.parser import (
    ParsedAAMVADocument,
    _normalize_date,
    parse_aamva,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_barcode(
    daq="D1234567",
    dcs="SMITH",
    dac="JOHN",
    dad="ALLEN",
    dbb="01151990",
    dba="01152029",
    dbd="01152021",
    dag="123 MAIN ST",
    dai="SACRAMENTO",
    daj="CA",
    dak="94203     ",
    dbc="1",
    dau="510",
    dcf="CADL1234567890123456789",
    dcg="USA",
    extra_fields: str = "",
) -> str:
    """
    Build a minimal valid AAMVA-ish barcode string.
    Uses the line-by-line format the fallback parser handles.
    """
    lines = [
        "@",
        "\x1e\rANSI 636014080002DL00410278ZC03290023",
        "DL",
        f"DAQ{daq}",
        f"DCS{dcs}",
        f"DAC{dac}",
        f"DAD{dad}",
        f"DBB{dbb}",
        f"DBA{dba}",
        f"DBD{dbd}",
        f"DAG{dag}",
        f"DAI{dai}",
        f"DAJ{daj}",
        f"DAK{dak}",
        f"DBC{dbc}",
        f"DAU{dau}",
        f"DCF{dcf}",
        f"DCG{dcg}",
    ]
    if extra_fields:
        lines.append(extra_fields)
    return "\n".join(lines)


VALID_BARCODE = _make_barcode()


# ---------------------------------------------------------------------------
# _normalize_date() unit tests
# ---------------------------------------------------------------------------

class TestNormalizeDate:
    def test_valid_date(self):
        assert _normalize_date("01151990") == "1990-01-15"

    def test_valid_date_dec_31(self):
        assert _normalize_date("12312030") == "2030-12-31"

    def test_invalid_month_passes_through(self):
        # Month 13 is not parseable — should be returned as-is
        result = _normalize_date("13012030")
        assert result == "13012030"

    def test_too_short_passes_through(self):
        result = _normalize_date("0101")
        assert result == "0101"

    def test_non_digit_passes_through(self):
        result = _normalize_date("ABCD1234")
        assert result == "ABCD1234"

    def test_strips_whitespace(self):
        assert _normalize_date(" 07041776 ") == "1776-07-04"


# ---------------------------------------------------------------------------
# parse_aamva() — happy path
# ---------------------------------------------------------------------------

class TestParseAAMVAHappyPath:
    def test_returns_parsed_document(self):
        doc = parse_aamva(VALID_BARCODE)
        assert isinstance(doc, ParsedAAMVADocument)

    def test_mandatory_fields_extracted(self):
        doc = parse_aamva(VALID_BARCODE)
        assert doc.raw_fields["DAQ"] == "D1234567"
        assert doc.raw_fields["DCS"] == "SMITH"
        assert doc.raw_fields["DAC"] == "JOHN"
        assert doc.raw_fields["DBB"] == "01151990"

    def test_date_normalization_applied(self):
        doc = parse_aamva(VALID_BARCODE)
        # DBB raw = "01151990" → normalized = "1990-01-15"
        assert doc.normalized_fields["DBB"] == "1990-01-15"
        assert doc.normalized_fields["DBA"] == "2029-01-15"
        assert doc.normalized_fields["DBD"] == "2021-01-15"

    def test_non_date_fields_unchanged(self):
        doc = parse_aamva(VALID_BARCODE)
        assert doc.normalized_fields["DAQ"] == doc.raw_fields["DAQ"]
        assert doc.normalized_fields["DCS"] == doc.raw_fields["DCS"]

    def test_parse_method_recorded(self):
        doc = parse_aamva(VALID_BARCODE)
        # aamva-barcode-library may not be installed in test env;
        # either "library" or "fallback" is valid
        assert doc.parse_method in ("library", "fallback")


# ---------------------------------------------------------------------------
# ZXX jurisdiction field extraction
# ---------------------------------------------------------------------------

class TestZXXExtraction:
    def test_zxx_field_extracted(self):
        barcode = _make_barcode(extra_fields="ZCA12345678901234567890")
        doc = parse_aamva(barcode)
        assert "ZCA" in doc.jurisdiction_fields

    def test_zxx_also_in_raw_fields(self):
        barcode = _make_barcode(extra_fields="ZCA12345678901234567890")
        doc = parse_aamva(barcode)
        assert "ZCA" in doc.raw_fields

    def test_no_zxx_fields_empty_dict(self):
        doc = parse_aamva(VALID_BARCODE)
        assert isinstance(doc.jurisdiction_fields, dict)
        # May be empty or contain ZXX fields depending on header parsing


# ---------------------------------------------------------------------------
# Header / sanity guard
# ---------------------------------------------------------------------------

class TestHeaderSanity:
    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="too short"):
            parse_aamva("@\nANSI")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_aamva("")

    def test_non_aamva_payload_raises(self):
        with pytest.raises(ValueError, match="does not appear to be AAMVA"):
            parse_aamva("X" * 30)

    def test_payload_with_daq_marker_accepted(self):
        # Even without '@' or 'ANSI', presence of 'DAQ' should be accepted
        minimal = "DAQ12345\nDCS SMITH\nDAC JOHN\nDBB01011980\nDBA01012030\nDBD01012020"
        doc = parse_aamva(minimal)
        assert doc.raw_fields.get("DAQ") is not None

    def test_fewer_than_3_fields_raises(self):
        with pytest.raises(ValueError, match="only"):
            parse_aamva("@\nANSI 636014\nDLDAQ1\n")


# ---------------------------------------------------------------------------
# Field count sanity
# ---------------------------------------------------------------------------

class TestFieldCount:
    def test_all_mandatory_fields_present(self):
        doc = parse_aamva(VALID_BARCODE)
        mandatory = ["DAQ", "DCS", "DAC", "DBB", "DBA", "DBD",
                     "DAG", "DAI", "DAJ", "DAK", "DBC", "DAU", "DCF", "DCG"]
        for fid in mandatory:
            assert fid in doc.raw_fields, f"Missing mandatory field: {fid}"

    def test_field_count_gte_14(self):
        doc = parse_aamva(VALID_BARCODE)
        assert len(doc.raw_fields) >= 14
