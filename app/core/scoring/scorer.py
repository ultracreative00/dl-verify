"""
Risk Scorer
===========
Aggregates ValidationResult objects from all cross-validation checks
into a single risk_score (float 0.0–1.0) and a recommendation string
(PASS | REVIEW | REJECT).

Public API
----------
    score(results: List[ValidationResult]) -> ScoringResult

ScoringResult
-------------
    risk_score     : float          0.0 = clean, 1.0 = certain fraud
    recommendation : str            "PASS" | "REVIEW" | "REJECT"
    score_breakdown: Dict           per-check contribution scores
    fired_hard_fails: List[str]     hard-fail signals that triggered
    fired_hard_warns: List[str]     hard-warn signals that triggered
    check_severities: Dict          check_name -> severity string
    all_signals    : Dict           flat merged dict of all signals

Scoring algorithm
-----------------
For each check:
  1. Enumerate the check's signals dict.
  2. For each signal that indicates a problem (truthy non-meta value),
     look up its SIGNAL_WEIGHT (default 0.5).
  3. The check's raw score = mean of firing signal weights  (0.0 if none fire).
  4. Multiply by CHECK_WEIGHT for this check.
  5. Normalise across all checks by their weight sum.

The final risk_score is the normalised weighted mean.
Hard-fail and hard-warn signals apply post-scoring overrides.

This is a transparent rule-based scorer.  Weights are in config.py.
Transition to ML (XGBoost) is planned for Sprint 5 when labelled data
is available; the interface (List[ValidationResult] -> ScoringResult)
will not change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.core.barcode.validators import ValidationResult
from app.core.scoring.config import (
    CHECK_WEIGHTS,
    HARD_FAIL_SIGNALS,
    HARD_WARN_SIGNALS,
    PASS_MAX,
    REVIEW_MAX,
    SIGNAL_WEIGHTS,
)
from app.utils.logger import logger


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

@dataclass
class ScoringResult:
    """
    Aggregated fraud risk assessment.

    Attributes
    ----------
    risk_score      : 0.0 (clean) to 1.0 (certain fraud)
    recommendation  : "PASS" | "REVIEW" | "REJECT"
    score_breakdown : per-check weighted contribution (0.0–1.0)
    fired_hard_fails: list of hard-fail signal keys that triggered
    fired_hard_warns: list of hard-warn signal keys that triggered
    check_severities: check_name -> "pass" | "warn" | "fail"
    all_signals     : flat merged dict of all signals from all checks
    """
    risk_score: float
    recommendation: str
    score_breakdown: Dict[str, float] = field(default_factory=dict)
    fired_hard_fails: List[str] = field(default_factory=list)
    fired_hard_warns: List[str] = field(default_factory=list)
    check_severities: Dict[str, str] = field(default_factory=dict)
    all_signals: Dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_META_SIGNALS = {
    # Signals that are purely informational; never treated as fraud indicators
    "rules_available",
    "dates_parseable",
    "mandatory_fields_present",
    "total_fields_found",
    "zxx_fields_found",
    "jurisdiction",
    "expected_windows",
    "span_years",
    "dcf_length",
    "dcf_pattern_match",   # handled explicitly below
    "age_fields_checked",
}


def _signal_fires(key: str, value: object) -> bool:
    """
    Return True if this signal represents a problem.

    Rules:
    - Meta signals never fire.
    - bool True  -> fires  ("window_mismatch": True)
    - bool False -> does not fire
    - Non-empty list -> fires  ("missing_mandatory_fields": ["DAQ", ...])
    - Empty list  -> does not fire
    - None  -> does not fire
    - Numeric > 0 -> fires  ("DDH_delta_days": 45)
    - String  -> fires (unexpected string values are anomalies)
    """
    if key in _META_SIGNALS:
        return False
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        return len(value) > 0
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        return True
    return bool(value)


def _check_bucket_score(result: ValidationResult) -> float:
    """
    Compute a 0.0–1.0 score for a single check based on its signals.

    Returns 0.0 if no signals fire (clean check).
    Returns the mean weight of all firing signals otherwise.

    Special handling:
    - dcf_pattern_match == False sets "dcf_pattern_match_false" synthetic
      signal so it can be caught by HARD_WARN_SIGNALS.
    """
    firing_weights: List[float] = []

    for key, value in result.signals.items():
        # Special case: dcf_pattern_match=False is a failure
        if key == "dcf_pattern_match" and value is False:
            firing_weights.append(SIGNAL_WEIGHTS.get("dcf_pattern_match_false", 0.8))
            continue

        if not _signal_fires(key, value):
            continue

        weight = SIGNAL_WEIGHTS.get(key, 0.5)  # default mid-weight for unknown signals
        firing_weights.append(weight)

    if not firing_weights:
        return 0.0

    return sum(firing_weights) / len(firing_weights)


def _collect_hard_signals(
    all_signals: Dict[str, object],
) -> tuple[List[str], List[str]]:
    """
    Scan the flat merged signal dict for hard-fail and hard-warn keys.
    Returns (fired_hard_fails, fired_hard_warns).
    """
    fired_fails = []
    fired_warns = []

    for key in HARD_FAIL_SIGNALS:
        value = all_signals.get(key)
        if _signal_fires(key, value):
            fired_fails.append(key)
        # Also check synthetic dcf_pattern_match_false
        if key == "dcf_pattern_match_false":
            if all_signals.get("dcf_pattern_match") is False:
                fired_fails.append(key)

    for key in HARD_WARN_SIGNALS:
        value = all_signals.get(key)
        if _signal_fires(key, value):
            fired_warns.append(key)
        if key == "dcf_pattern_match_false":
            if all_signals.get("dcf_pattern_match") is False:
                fired_warns.append(key)

    return fired_fails, fired_warns


def _recommend(risk_score: float, hard_fails: List[str], hard_warns: List[str]) -> str:
    """
    Derive recommendation from numeric score + hard override signals.

    Priority order:
      1. Any hard-fail signal -> REJECT  (overrides score)
      2. Any hard-warn signal -> at least REVIEW  (lifts floor)
      3. Numeric score bands  -> PASS / REVIEW / REJECT
    """
    if hard_fails:
        return "REJECT"

    # Numeric band
    if risk_score <= PASS_MAX:
        rec = "PASS"
    elif risk_score <= REVIEW_MAX:
        rec = "REVIEW"
    else:
        rec = "REJECT"

    # Hard-warn floor lift
    if hard_warns and rec == "PASS":
        rec = "REVIEW"

    return rec


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def score(results: List[ValidationResult]) -> ScoringResult:
    """
    Aggregate a list of ValidationResult objects into a ScoringResult.

    Parameters
    ----------
    results : list of ValidationResult from the six cross-validation checks
              (and any additional checks from Sprint 2+)

    Returns
    -------
    ScoringResult
    """
    if not results:
        logger.warning("scorer_empty_input", msg="score() called with no ValidationResults")
        return ScoringResult(
            risk_score=1.0,
            recommendation="REJECT",
            score_breakdown={},
            fired_hard_fails=["no_checks_run"],
            fired_hard_warns=[],
            check_severities={},
            all_signals={},
        )

    # --- Merge all signals into a flat dict ---
    # Later checks overwrite earlier ones on key collision; this is intentional
    # because more-specific checks (e.g. ocr_barcode_diff) should dominate.
    all_signals: Dict[str, object] = {}
    check_severities: Dict[str, str] = {}
    for r in results:
        all_signals.update(r.signals)
        check_severities[r.check] = r.severity

    # --- Per-check bucket scores ---
    score_breakdown: Dict[str, float] = {}
    weighted_sum = 0.0
    weight_total = 0.0

    for r in results:
        check_weight = CHECK_WEIGHTS.get(r.check, 0.5)  # default weight for unknown checks
        bucket_score = _check_bucket_score(r)
        weighted_contribution = check_weight * bucket_score
        score_breakdown[r.check] = round(bucket_score, 4)
        weighted_sum += weighted_contribution
        weight_total += check_weight

    # Normalise to 0.0–1.0
    risk_score = (weighted_sum / weight_total) if weight_total > 0 else 0.0
    risk_score = round(min(max(risk_score, 0.0), 1.0), 4)

    # --- Hard override signals ---
    fired_fails, fired_warns = _collect_hard_signals(all_signals)

    # --- Final recommendation ---
    recommendation = _recommend(risk_score, fired_fails, fired_warns)

    result = ScoringResult(
        risk_score=risk_score,
        recommendation=recommendation,
        score_breakdown=score_breakdown,
        fired_hard_fails=fired_fails,
        fired_hard_warns=fired_warns,
        check_severities=check_severities,
        all_signals=all_signals,
    )

    logger.info(
        "risk_score",
        risk_score=risk_score,
        recommendation=recommendation,
        hard_fails=fired_fails,
        hard_warns=fired_warns,
        checks_run=len(results),
    )

    return result
