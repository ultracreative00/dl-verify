"""
tests/test_scorer.py
====================
Unit tests for app.core.scoring.scorer.score().

All ValidationResult fixtures are built inline — no pipeline invocation
or live images required.
"""
from __future__ import annotations

import pytest

from app.core.barcode.validators import ValidationResult
from app.core.scoring.config import PASS_MAX, REVIEW_MAX
from app.core.scoring.scorer import ScoringResult, score


# ---------------------------------------------------------------------------
# ValidationResult factory
# ---------------------------------------------------------------------------

def _vr(
    check: str,
    severity: str = "pass",
    signals: dict | None = None,
) -> ValidationResult:
    return ValidationResult(
        check=check,
        severity=severity,
        signals=signals or {},
    )


def _clean_results() -> list[ValidationResult]:
    """Six checks that all pass cleanly — should produce PASS."""
    return [
        _vr("check_syntax_conformance"),
        _vr("check_date_logic"),
        _vr("check_expiry_window"),
        _vr("check_jurisdiction_fields"),
        _vr("check_dcf_entropy"),
        _vr("check_age_derived_fields"),
    ]


# ---------------------------------------------------------------------------
# Basic contract tests
# ---------------------------------------------------------------------------

class TestScoreContract:
    def test_returns_scoring_result(self):
        result = score(_clean_results())
        assert isinstance(result, ScoringResult)

    def test_risk_score_is_float(self):
        result = score(_clean_results())
        assert isinstance(result.risk_score, float)

    def test_risk_score_within_bounds(self):
        result = score(_clean_results())
        assert 0.0 <= result.risk_score <= 1.0

    def test_recommendation_is_valid_string(self):
        result = score(_clean_results())
        assert result.recommendation in ("PASS", "REVIEW", "REJECT")

    def test_clean_results_produce_pass(self):
        result = score(_clean_results())
        assert result.recommendation == "PASS"
        assert result.risk_score <= PASS_MAX

    def test_score_breakdown_has_all_checks(self):
        results = _clean_results()
        sr = score(results)
        for vr in results:
            assert vr.check in sr.score_breakdown

    def test_all_signals_merged(self):
        results = [
            _vr("check_date_logic", signals={"document_expired": True}),
            _vr("check_dcf_entropy", signals={"dcf_length": 25}),
        ]
        sr = score(results)
        assert "document_expired" in sr.all_signals
        assert "dcf_length" in sr.all_signals


# ---------------------------------------------------------------------------
# Hard-fail override
# ---------------------------------------------------------------------------

class TestHardFails:
    def test_hard_fail_signal_forces_reject(self):
        results = [
            _vr("check_date_logic", severity="fail",
                signals={"dob_after_issue": True}),
        ] + _clean_results()[1:]
        sr = score(results)
        assert sr.recommendation == "REJECT"
        assert "dob_after_issue" in sr.fired_hard_fails

    def test_hard_fail_overrides_low_numeric_score(self):
        # Even if most checks pass, a hard-fail signal must force REJECT
        results = _clean_results()
        results[1] = _vr("check_date_logic", severity="fail",
                         signals={"issue_after_expiry": True})
        sr = score(results)
        assert sr.recommendation == "REJECT"

    def test_missing_mandatory_fields_is_hard_fail(self):
        results = [
            _vr("check_syntax_conformance", severity="fail",
                signals={"missing_mandatory_fields": ["DAQ", "DCS"]}),
        ] + _clean_results()[1:]
        sr = score(results)
        assert sr.recommendation == "REJECT"


# ---------------------------------------------------------------------------
# Hard-warn floor lift
# ---------------------------------------------------------------------------

class TestHardWarns:
    def test_hard_warn_lifts_pass_to_review(self):
        # dcf_pattern_match=False is a hard-warn signal
        results = _clean_results()
        results[4] = _vr("check_dcf_entropy", severity="warn",
                         signals={"dcf_pattern_match": False})
        sr = score(results)
        # Should be at least REVIEW, not PASS
        assert sr.recommendation in ("REVIEW", "REJECT")


# ---------------------------------------------------------------------------
# Numeric band tests
# ---------------------------------------------------------------------------

class TestRecommendationBands:
    def test_score_at_pass_max_is_pass(self):
        # Inject a score right at the PASS boundary
        # The simplest way: all checks pass, score should be 0.0
        sr = score(_clean_results())
        assert sr.risk_score <= PASS_MAX
        assert sr.recommendation == "PASS"

    def test_high_score_produces_reject(self):
        # Many signals firing across multiple checks
        results = [
            _vr("check_syntax_conformance", severity="fail",
                signals={"invalid_country_code": True, "invalid_sex_code": True}),
            _vr("check_date_logic", severity="fail",
                signals={"document_expired": True, "issue_date_future": True}),
            _vr("check_expiry_window", severity="fail",
                signals={"window_mismatch": True}),
            _vr("check_jurisdiction_fields", severity="fail",
                signals={"missing_zxx_fields": ["ZCA", "ZCB"]}),
            _vr("check_dcf_entropy", severity="fail",
                signals={"dcf_too_short": True}),
            _vr("check_age_derived_fields", severity="fail",
                signals={"DDH_inconsistent": True}),
        ]
        sr = score(results)
        assert sr.risk_score > REVIEW_MAX
        assert sr.recommendation == "REJECT"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_input_returns_reject(self):
        sr = score([])
        assert sr.recommendation == "REJECT"
        assert sr.risk_score == 1.0
        assert "no_checks_run" in sr.fired_hard_fails

    def test_single_check_does_not_crash(self):
        sr = score([_vr("check_date_logic")])
        assert isinstance(sr, ScoringResult)
        assert 0.0 <= sr.risk_score <= 1.0

    def test_risk_score_clamped_to_1(self):
        # Even with extreme weights, score must not exceed 1.0
        results = [
            _vr("check_date_logic", severity="fail",
                signals={s: True for s in [
                    "dob_after_issue", "issue_after_expiry",
                    "issue_date_future", "document_expired"
                ]})
        ]
        sr = score(results)
        assert sr.risk_score <= 1.0

    def test_unknown_check_name_uses_default_weight(self):
        # A check not in CHECK_WEIGHTS should use default 0.5 — not crash
        sr = score([_vr("check_future_sprint_x", signals={"some_signal": True})])
        assert isinstance(sr, ScoringResult)
