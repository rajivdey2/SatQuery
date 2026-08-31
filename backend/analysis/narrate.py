"""Answer synthesis from measurements.

The narration layer is deliberately restricted: it may only restate quantities
that the analysis engine measured. Nothing here invents an object, a count or an
area. That restriction is the reason an answer from this system can be checked
against its own audit trace -- and it is why the system stays usable on the
ISRO/SAC evaluation imagery, where a fluent guess scores worse than a measured
statement with an honest caveat.

When a VLM backend is enabled, these same measurements are handed to it as tool
evidence (see ``specialists/backends/vlm.py``); the text below is the grounded
fallback and the reference the VLM answer is checked against.
"""
from __future__ import annotations

import re
from typing import List, Optional

from backend.analysis.grounding import parse_phrase
from backend.analysis.measurements import (BUILT_UP, CLASS_LABELS, CLASS_ORDER, OTHER,
                                           VEGETATION, WATER, ChangeMeasurement,
                                           FusionMeasurement, GroundingMeasurement,
                                           LandCoverMeasurement, summarize_classes)

_COUNT_Q = re.compile(r"\bhow many\b|\bnumber of\b|\bcount\b", re.I)
_EXTENT_Q = re.compile(r"\bhow much\b|\bwhat (?:percentage|percent|fraction|proportion|share)\b|"
                       r"\bhow large\b|\barea of\b|\bextent of\b|\bcoverage\b", re.I)
_PRESENCE_Q = re.compile(r"^\s*(is|are|does|do|can|has|have|was|were)\b|\bany\b|\bpresent\b|"
                         r"\bcontain\b|\bvisible\b", re.I)
_LOCATION_Q = re.compile(r"\bwhere\b|\bwhich (?:part|side|region|quadrant|corner)\b|\blocated\b", re.I)
_DOMINANT_Q = re.compile(r"\bdominant\b|\bmain land[- ]cover\b|\bmost of\b|\bmajority\b|"
                         r"\bland[- ]cover\b|\bland use\b", re.I)


def _pct(x: Optional[float]) -> str:
    return "unknown" if x is None else f"{x * 100:.1f}%"


def _area(measurement_area: Optional[float]) -> str:
    return f" ({measurement_area:.1f} ha)" if measurement_area else ""


def _caveats(m) -> List[str]:
    q = getattr(m, "quality", None)
    return list(q.limitations) if q and q.limitations else []


def _caveat_sentence(m, limit: int = 2) -> str:
    caveats = [c for c in _caveats(m) if c]
    if not caveats:
        return ""
    return " Caveats: " + " ".join(caveats[:limit])


def _scene_label_sentence(m: LandCoverMeasurement) -> str:
    if not m.scene_labels:
        return ""
    positives = [p for p in m.scene_labels if p.get("above_threshold")]
    shown = positives[:4] or m.scene_labels[:2]
    parts = ", ".join(f"{p['label']} ({p['probability']:.2f})" for p in shown)
    verb = "identifies" if positives else "ranks (none above its decision threshold)"
    return f" The BigEarthNet-adapted land-cover head {verb}: {parts}."


def describe_scene(m: LandCoverMeasurement, modality: str = "optical") -> str:
    """Scene description / captioning answer."""
    dominant = CLASS_LABELS.get(m.dominant, m.dominant)
    lead = f"The {modality} scene is dominated by {dominant}."
    extents = f" Measured extents: {summarize_classes(m.classes, top=4)}."
    structure = ""
    big = [r for r in m.regions if r.fraction > 0.01][:3]
    if big:
        bits = [f"{r.label} in the {r.position}" + _area(r.area_ha) for r in big]
        structure = " Notable regions: " + "; ".join(bits) + "."
    counts = [c for c in m.classes if c.region_count > 1 and c.name != OTHER][:2]
    count_txt = ""
    if counts:
        count_txt = " " + "; ".join(
            f"{c.region_count} separate {CLASS_LABELS.get(c.name, c.name)} regions" for c in counts) + "."
    return lead + extents + structure + count_txt + _scene_label_sentence(m) + _caveat_sentence(m)


def answer_single(query: str, m: LandCoverMeasurement, modality: str = "optical") -> str:
    """VQA answer for a single image, routed by question type."""
    parsed = parse_phrase(query)
    target = parsed.target_class
    stat = m.get(target) if target else None

    if _COUNT_Q.search(query or ""):
        if stat is None:
            return ("The question asks for a count, but the referenced feature is not one of the "
                    f"classes this system measures ({', '.join(CLASS_LABELS[c] for c in CLASS_ORDER[:4])}). "
                    "No count is reported." + _caveat_sentence(m))
        regions = [r for r in m.regions if r.class_name == target]
        shown = f" The largest covers {_pct(regions[0].fraction)} of the scene{_area(regions[0].area_ha)}." if regions else ""
        return (f"{stat.region_count} distinct {CLASS_LABELS.get(target, target)} region(s) are resolved "
                f"above the minimum region size.{shown} This counts contiguous regions of that class, "
                "not individual objects." + _caveat_sentence(m))

    if _EXTENT_Q.search(query or "") and stat is not None:
        return (f"{CLASS_LABELS.get(target, target)} covers {_pct(stat.fraction)} of the valid image area"
                f"{_area(stat.area_ha)}. Basis: {stat.evidence_basis}." + _caveat_sentence(m))

    if _LOCATION_Q.search(query or "") and target:
        regions = [r for r in m.regions if r.class_name == target]
        if not regions:
            return (f"No {CLASS_LABELS.get(target, target)} region was detected in this image, so there is "
                    "no location to report." + _caveat_sentence(m))
        bits = [f"{r.position}{_area(r.area_ha)}" for r in regions[:3]]
        return (f"{CLASS_LABELS.get(target, target)} is located in the {'; '.join(bits)}. "
                f"Total extent {_pct(stat.fraction if stat else None)}." + _caveat_sentence(m))

    if target and _PRESENCE_Q.search(query or ""):
        if stat is None or stat.fraction <= 0.002:
            return (f"No — {CLASS_LABELS.get(target, target)} is not detected above 0.2% of the scene. "
                    f"Dominant class is {CLASS_LABELS.get(m.dominant, m.dominant)} "
                    f"({_pct(m.fraction(m.dominant))})." + _caveat_sentence(m))
        return (f"Yes — {CLASS_LABELS.get(target, target)} covers {_pct(stat.fraction)} of the scene"
                f"{_area(stat.area_ha)}, in {stat.region_count} region(s). Basis: {stat.evidence_basis}."
                + _caveat_sentence(m))

    if _DOMINANT_Q.search(query or "") or not target:
        return describe_scene(m, modality)

    return (f"{CLASS_LABELS.get(target, target)}: {_pct(stat.fraction) if stat else 'not measured'} of the "
            f"scene{_area(stat.area_ha) if stat else ''}. Full breakdown — "
            f"{summarize_classes(m.classes, top=4)}." + _caveat_sentence(m))


def answer_grounding(m: GroundingMeasurement) -> str:
    """Grounding answer: what was localised, where, and how certain."""
    label = CLASS_LABELS.get(m.target_class, m.target_class)
    if not m.regions:
        return (f"No {label} region could be localised for \"{m.target_phrase}\" in this image; "
                "no bounding box is returned." + _caveat_sentence(m))
    parts = []
    for r in m.regions:
        box = ", ".join(f"{v:.1f}" for v in r.bbox_norm)
        parts.append(f"{r.position} of the scene, box [{box}] in 0-100 image coordinates"
                     f"{_area(r.area_ha)}")
    qual = f" ({m.qualifier})" if m.qualifier else ""
    head = (f"Localised {len(m.regions)} {label} region{'s' if len(m.regions) > 1 else ''}{qual} "
            f"for \"{m.target_phrase}\": " + "; ".join(parts) + ".")
    extra = ""
    if m.candidates_considered > len(m.regions):
        extra = (f" {m.candidates_considered} candidate regions of this class were considered before "
                 "the qualifiers were applied.")
    return head + extra + _caveat_sentence(m)


def answer_change(query: str, m: ChangeMeasurement) -> str:
    """Change-VQA answer: direction verdict first, then the numbers."""
    span = ""
    if m.t1_date and m.t2_date:
        span = f" between {m.t1_date} and {m.t2_date}"
    parsed = parse_phrase(query)
    target = parsed.target_class

    if target:
        d = m.delta(target)
        if d is None:
            return describe_change(m)
        label = CLASS_LABELS.get(target, target)
        if not d.significant:
            return (f"{label} has remained essentially unchanged{span}: "
                    f"{_pct(d.t1_fraction)} of the scene at the earlier date versus "
                    f"{_pct(d.t2_fraction)} at the later date "
                    f"(change {d.delta_fraction * 100:+.2f} percentage points"
                    f"{f', {d.delta_ha:+.1f} ha' if d.delta_ha is not None else ''}), which does not "
                    "clear the measurement noise floor." + _caveat_sentence(m))
        rel = f" ({d.relative_change * 100:+.1f}% relative)" if d.relative_change is not None else ""
        where = _change_location(m, target)
        return (f"{label} has {d.direction}{span}: {_pct(d.t1_fraction)} -> {_pct(d.t2_fraction)} of the "
                f"scene, a change of {d.delta_fraction * 100:+.2f} percentage points"
                f"{f' ({d.delta_ha:+.1f} ha)' if d.delta_ha is not None else ''}{rel}.{where}"
                + _caveat_sentence(m))

    return describe_change(m)


def _change_location(m: ChangeMeasurement, target: Optional[str] = None) -> str:
    rows = [t for t in m.transitions if target is None or t["to"] == target or t["from"] == target]
    if not rows:
        if m.clusters:
            c = m.clusters[0]
            return f" The largest changed cluster is in the {c.position}{_area(c.area_ha)}."
        return ""
    top = rows[0]
    return (f" The dominant transition is {CLASS_LABELS.get(top['from'], top['from'])} -> "
            f"{CLASS_LABELS.get(top['to'], top['to'])}, concentrated in the {top['location']}"
            f"{_area(top.get('area_ha'))}.")


def describe_change(m: ChangeMeasurement) -> str:
    """Change-description answer: what changed, how much, and where."""
    span = f" between {m.t1_date} and {m.t2_date}" if (m.t1_date and m.t2_date) else ""
    if m.changed_fraction <= 0.005:
        return (f"No substantial change is detected{span}: only {_pct(m.changed_fraction)} of the "
                f"jointly valid area exceeds the change threshold "
                f"({m.magnitude_threshold if m.magnitude_threshold is not None else 'n/a'}), "
                f"at or near the estimated noise floor "
                f"({m.noise_floor if m.noise_floor is not None else 'n/a'})." + _caveat_sentence(m))

    head = (f"{_pct(m.changed_fraction)} of the area changed{span}"
            f"{f' ({m.changed_area_ha:.1f} ha)' if m.changed_area_ha else ''}.")
    moved = [d for d in m.per_class if d.significant]
    moved.sort(key=lambda d: -abs(d.delta_fraction))
    trend = ""
    if moved:
        bits = [f"{CLASS_LABELS.get(d.name, d.name)} {d.direction} by "
                f"{abs(d.delta_fraction) * 100:.2f} percentage points"
                f"{f' ({d.delta_ha:+.1f} ha)' if d.delta_ha is not None else ''}" for d in moved[:3]]
        trend = " Net class changes: " + "; ".join(bits) + "."
    trans = ""
    if m.transitions:
        bits = [f"{CLASS_LABELS.get(t['from'], t['from'])} -> {CLASS_LABELS.get(t['to'], t['to'])} "
                f"({_pct(t['fraction'])}, {t['location']})" for t in m.transitions[:3]]
        trans = " Dominant transitions: " + "; ".join(bits) + "."
    where = ""
    if m.clusters:
        bits = [f"{c.position}{_area(c.area_ha)}" for c in m.clusters[:3]]
        where = " Change clusters: " + "; ".join(bits) + "."
    return head + trend + trans + where + _caveat_sentence(m)


def answer_fusion(query: str, m: FusionMeasurement) -> str:
    """Optical+SAR joint answer, foregrounding what each sensor contributed."""
    water = next((c for c in m.joint_classes if c.name == WATER), None)
    built = next((c for c in m.joint_classes if c.name == BUILT_UP), None)
    head_bits = []
    if built:
        head_bits.append(f"built-up / man-made surface {_pct(built.fraction)}{_area(built.area_ha)}")
    if water:
        head_bits.append(f"water-covered {_pct(water.fraction)}{_area(water.area_ha)}")
    veg = next((c for c in m.joint_classes if c.name == VEGETATION), None)
    if veg and veg.fraction > 0.01:
        head_bits.append(f"vegetation {_pct(veg.fraction)}{_area(veg.area_ha)}")
    head = ("Using the optical and SAR images jointly: " + "; ".join(head_bits) + "."
            if head_bits else "The joint optical+SAR pass produced no class above the reporting floor.")

    agree_bits = []
    for s in m.agreement:
        if s.cohen_kappa is None:
            continue
        agree_bits.append(f"{CLASS_LABELS.get(s.class_name, s.class_name)} kappa {s.cohen_kappa:.2f} "
                          f"(optical {_pct(s.optical_fraction)}, SAR {_pct(s.sar_fraction)})")
    agree = ""
    if agree_bits:
        agree = (f" Cross-modal agreement — " + "; ".join(agree_bits) +
                 (f"; mean kappa {m.mean_agreement:.2f}." if m.mean_agreement is not None else "."))

    comp = ""
    if m.complementarity:
        comp = " " + " ".join(m.complementarity[:2])
    elif m.obscured_fraction > 0.01:
        comp = (f" {_pct(m.obscured_fraction)} of the optical scene is obscured; the SAR channel "
                "carries the classification there.")

    where = ""
    top = [r for r in m.regions][:3]
    if top:
        where = " Largest joint regions: " + "; ".join(
            f"{r.label} in the {r.position}{_area(r.area_ha)}" for r in top) + "."

    labels = ""
    if m.optical and m.optical.scene_labels:
        labels = _scene_label_sentence(m.optical)

    return head + agree + comp + where + labels + _caveat_sentence(m)


def one_line_summary(task: str, text: str, limit: int = 240) -> str:
    """Short form used in list views and the PDF header."""
    flat = re.sub(r"\s+", " ", text or "").strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"
