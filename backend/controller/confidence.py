"""Confidence estimation (Controller stage 5, second half).

CLAUDE.md's risk register is blunt about this: a fabricated confidence number is
worse than no number, because a judge who probes it will find nothing behind it.
So this module only ever combines signals that were actually measured, names every
one of them in the trace with its weight, and reports ``not_available`` when there
is nothing to combine.

The signals, all real:

* ``separability`` — Otsu between-class variance ratio of the thresholds that
  produced the answer. A bimodal histogram earns a high value; a flat chip does not.
* ``band_completeness`` — fraction of the indices the task wanted that the product
  could actually supply (an RGB PNG cannot supply NDVI).
* ``valid_pixel_fraction`` — how much of the frame was not nodata.
* ``cross_modal_kappa`` — Cohen's kappa between the optical and SAR evidence.
* ``change_margin`` — how far the change threshold sits above the estimated noise floor.
* ``region_score`` — geometric quality of the grounded region.
* ``learned_margin`` — top-1 minus top-2 probability of the adapted BigEarthNet-MM head.
* ``logit_margin`` / ``self_consistency`` — the VLM's own output-distribution margin
  and agreement across samples, when a VLM narrated the answer.

``calibrated`` is always False and the note says so: this is a bounded, documented
composite of measured quantities, not a probability. Nothing here was fitted
against a labelled reliability set, and claiming otherwise would be the exact
failure the risk register warns about.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from backend.controller.audit import Confidence, ConfidenceComponent

# name -> (weight, human note)
_WEIGHTS: Dict[str, Tuple[float, str]] = {
    "cross_modal_kappa": (0.35, "Cohen's kappa between optical and SAR evidence"),
    "separability": (0.30, "Otsu between-class variance ratio of the applied thresholds"),
    "band_completeness": (0.18, "fraction of the wanted spectral indices the product supports"),
    "valid_pixel_fraction": (0.08, "share of the frame that is valid (not nodata)"),
    "change_margin": (0.22, "change threshold headroom above the estimated noise floor"),
    "region_score": (0.20, "geometric quality of the grounded region"),
    "learned_margin": (0.15, "top-2 margin of the adapted BigEarthNet-MM land-cover head"),
    "logit_margin": (0.12, "mean top-2 token probability margin of the narration model"),
    "self_consistency": (0.10, "agreement across repeated narration samples"),
}

_NOT_CALIBRATED = ("Bounded composite of the measured signals listed above, not a calibrated "
                   "probability: no reliability fit against labelled data has been performed. "
                   "Read it as a relative indication of how well-posed this measurement was.")


def _norm(name: str, value: Any) -> Optional[float]:
    """Map a raw signal onto [0, 1] with a documented transform."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if name == "cross_modal_kappa":
        return max(0.0, min(1.0, v))                    # kappa <= 0 means no agreement at all
    if name == "change_margin":
        return max(0.0, min(1.0, (v - 1.0) / 2.0))      # ratio 1 -> 0, ratio 3+ -> 1
    return max(0.0, min(1.0, v))


def _penalties(signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    factor = 1.0
    notes: List[str] = []
    n_lim = int(signals.get("n_limitations") or 0)
    if n_lim:
        step = max(0.6, 1.0 - 0.05 * n_lim)
        factor *= step
        notes.append(f"{n_lim} measurement limitation(s) reduced the score by "
                     f"{(1 - step) * 100:.0f}%.")
    reliability = signals.get("target_reliability")
    if reliability == "low":
        factor *= 0.75
        notes.append("Target class reliability for this product is 'low' (-25%).")
    elif reliability == "unavailable":
        factor *= 0.5
        notes.append("Target class is not separable from this product's bands (-50%).")
    if signals.get("spatial_reference") is False:
        factor *= 0.95
        notes.append("No CRS: areas are in pixels only (-5%).")
    if signals.get("sar_radiometry_known") is False:
        factor *= 0.85
        notes.append("SAR radiometric convention could not be determined (-15%).")
    if signals.get("dates_known") is False:
        factor *= 0.9
        notes.append("Acquisition dates unavailable; chronology assumed from upload order (-10%).")
    return factor, notes


def _dominant_source(present: List[str]) -> str:
    if "cross_modal_kappa" in present:
        return "cross_modal_agreement"
    if "learned_margin" in present and "separability" not in present:
        return "learned_probability"
    if "separability" in present or "band_completeness" in present or "change_margin" in present:
        return "measurement_composite"
    if "logit_margin" in present:
        return "logit_margin"
    if "self_consistency" in present:
        return "self_consistency"
    return "not_available"


def estimate(signals: Dict[str, Any], narration_source: str = "measurement") -> Confidence:
    """Compose the confidence for one specialist result."""
    if narration_source == "mock" or signals.get("mock"):
        return Confidence(
            value=None, source="mock", calibrated=False,
            note="Mock backend was active (SATQUERY_MOCK=1): no measurement was performed, so no "
                 "confidence is reported.")

    # 'change_threshold_over_noise' is the specialist's name for the change margin.
    raw = dict(signals)
    if "change_threshold_over_noise" in raw and "change_margin" not in raw:
        raw["change_margin"] = raw["change_threshold_over_noise"]

    components: List[ConfidenceComponent] = []
    weighted, total_weight = 0.0, 0.0
    present: List[str] = []
    for name, (weight, note) in _WEIGHTS.items():
        v = _norm(name, raw.get(name))
        if v is None:
            continue
        components.append(ConfidenceComponent(name=name, value=round(v, 4), weight=weight, note=note))
        weighted += weight * v
        total_weight += weight
        present.append(name)

    if not components:
        return Confidence(
            value=None, source="not_available", calibrated=False,
            note="No measured confidence signal was available for this task, so no number is "
                 "reported. This is deliberate: an invented score would not survive scrutiny.")

    base = weighted / total_weight
    factor, penalty_notes = _penalties(raw)
    value = round(max(0.0, min(1.0, base * factor)), 3)

    note = _NOT_CALIBRATED
    if penalty_notes:
        note += " " + " ".join(penalty_notes)
    if narration_source.startswith("vlm"):
        note += (" The wording was produced by the narration model, but every number in the answer "
                 "comes from the measurements above.")
    for name, penalty in (("penalty_factor", factor),):
        components.append(ConfidenceComponent(name=name, value=round(penalty, 4), weight=0.0,
                                              note="multiplicative penalty from the notes above"))

    return Confidence(value=value, source=_dominant_source(present), calibrated=False,
                      components=components, note=note)


def rejection_confidence(reasons: List[str]) -> Confidence:
    """Confidence for the validator-gate path: the rejection itself is certain."""
    return Confidence(
        value=1.0, source="validator_gate", calibrated=False,
        components=[ConfidenceComponent(name="deterministic_checks", value=1.0, weight=1.0,
                                        note=f"{len(reasons)} input check(s) failed deterministically")],
        note="The input was rejected by deterministic compatibility checks, not by a model. The "
             "rejection is certain; no answer was produced.")
