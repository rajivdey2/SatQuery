"""Optical + SAR joint extraction (cross-modal specialist).

CLAUDE.md section 6 flags this as the component most teams do worst and the one
worth leaning into. What makes an answer here *joint* rather than two separate
answers stapled together is that the two modalities are measured independently
and then compared: per-class Cohen's kappa and IoU between the optical evidence
and the radar evidence, an explicit account of where SAR sees through cloud that
optical cannot, and a joint map whose provenance is recorded per class.

The cross-modal agreement is also the most defensible confidence signal in the
whole system -- two physically independent sensors agreeing on where the water is
means something, and when they disagree the system says so instead of averaging
the disagreement away.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np

from backend.analysis.indices import IndexStack, compute_indices
from backend.analysis.landcover import measure_land_cover
from backend.analysis.measurements import (BUILT_UP, CLASS_LABELS, OTHER, VEGETATION, WATER,
                                           AgreementStat, ClassStat, FusionMeasurement,
                                           QualityReport, Region, class_stat)
from backend.analysis.regions import label_regions, smooth_mask
from backend.config import settings
from backend.preprocessing.pipeline import PreparedImage, align_pair

# Classes both sensors can independently speak about.
_JOINT_CLASSES = (WATER, BUILT_UP, VEGETATION)


def _kappa(a: np.ndarray, b: np.ndarray, valid: np.ndarray) -> Optional[float]:
    """Cohen's kappa between two binary masks over the valid area."""
    n = int(valid.sum())
    if n < 64:
        return None
    x, y = a[valid], b[valid]
    po = float(np.mean(x == y))
    px, py = float(x.mean()), float(y.mean())
    pe = px * py + (1.0 - px) * (1.0 - py)
    if pe >= 1.0 - 1e-9:
        return 1.0 if po >= 1.0 - 1e-9 else 0.0
    return round(float((po - pe) / (1.0 - pe)), 4)


def _iou(a: np.ndarray, b: np.ndarray) -> Optional[float]:
    union = int((a | b).sum())
    if union == 0:
        return None
    return round(float(int((a & b).sum()) / union), 4)


def detect_obscured(img: PreparedImage, stack: IndexStack) -> Tuple[np.ndarray, str]:
    """Cloud / haze mask for the optical scene -- where SAR has to take over.

    Cloud is bright in every visible band, radiometrically smooth, and has no
    vegetation signal. This is a screening test, not a full cloud mask: it exists
    so the fusion step can say *which* pixels the optical sensor could not see.
    """
    bright, tex = stack.get("BRIGHTNESS"), stack.get("TEXTURE")
    if bright is None:
        return np.zeros(img.shape, dtype=bool), "no brightness channel: obscuration not assessed"
    valid = img.valid
    pool = bright[valid]
    pool = pool[np.isfinite(pool)]
    if pool.size < 64:
        return np.zeros(img.shape, dtype=bool), "too few valid pixels to assess obscuration"
    hi = float(np.percentile(pool, 97))
    cut = max(hi, 0.85)
    mask = bright >= cut
    if tex is not None:
        tpool = tex[valid]
        tpool = tpool[np.isfinite(tpool)]
        if tpool.size:
            mask &= tex <= float(np.percentile(tpool, 60))
    ndvi = stack.get("NDVI")
    if ndvi is not None:
        mask &= ndvi < 0.1
    mask &= valid
    mask = smooth_mask(mask, win=3)
    frac = float(mask.mean())
    note = (f"{frac * 100:.1f}% of the optical scene is bright, smooth and non-vegetated "
            f"(brightness >= {cut:.2f}): treated as cloud/haze obscured")
    return mask, note


def measure_fusion(optical: PreparedImage, sar: PreparedImage,
                   min_region_pixels: Optional[int] = None,
                   smoothing_window: Optional[int] = None,
                   regions_per_class: int = 4) -> Tuple[FusionMeasurement, Dict[str, np.ndarray]]:
    """Joint optical+SAR extraction with per-class cross-modal agreement."""
    sar_aligned = align_pair(optical, sar)
    notes: List[str] = []
    if sar_aligned is not sar:
        notes.append(f"SAR image resampled from {sar.shape[0]}x{sar.shape[1]} to "
                     f"{sar_aligned.shape[0]}x{sar_aligned.shape[1]} onto the optical grid.")

    lc_opt = measure_land_cover(optical, min_region_pixels=min_region_pixels,
                                smoothing_window=smoothing_window, with_scene_labels=False)
    lc_sar = measure_land_cover(sar_aligned, min_region_pixels=min_region_pixels,
                                smoothing_window=smoothing_window, with_scene_labels=False)
    valid = optical.valid & sar_aligned.valid
    total = int(valid.sum())

    obscured, obscured_note = detect_obscured(optical, lc_opt.stack)
    obscured &= valid
    notes.append(obscured_note)

    agreement: List[AgreementStat] = []
    joint_masks: Dict[str, np.ndarray] = {}
    provenance: Dict[str, str] = {}
    complementarity: List[str] = []

    for name in _JOINT_CLASSES:
        a = lc_opt.mask(name) & valid
        b = lc_sar.mask(name) & valid
        sar_speaks = _sar_can_speak(name, lc_sar.stack)
        opt_speaks = _optical_can_speak(name, lc_opt.stack)

        confirmed = a & b
        opt_only = a & ~b
        sar_only = b & ~a
        joint = confirmed | (sar_only & obscured) | (opt_only & ~obscured)
        if not sar_speaks:
            joint = a
            provenance[name] = ("optical only: the SAR product carries no discriminator for this class "
                                "(single-polarisation)")
        elif not opt_speaks:
            joint = b
            provenance[name] = "SAR only: the optical product carries no discriminator for this class"
        else:
            provenance[name] = ("cross-modal: pixels confirmed by both sensors, plus SAR-only detections "
                                "where the optical scene is obscured, plus optical-only detections in "
                                "clear areas")
        joint = smooth_mask(joint & valid, win=(settings.smoothing_window
                                                if smoothing_window is None else int(smoothing_window)))
        joint_masks[name] = joint & valid

        stat = AgreementStat(
            class_name=name,
            optical_fraction=(round(float(a.sum()) / total, 5) if total and opt_speaks else None),
            sar_fraction=(round(float(b.sum()) / total, 5) if total and sar_speaks else None),
            joint_fraction=round(float(joint_masks[name].sum()) / total, 5) if total else 0.0,
            iou=(_iou(a, b) if (sar_speaks and opt_speaks) else None),
            cohen_kappa=(_kappa(a, b, valid) if (sar_speaks and opt_speaks) else None),
            sar_only_fraction=round(float(sar_only.sum()) / total, 5) if total else 0.0,
            optical_only_fraction=round(float(opt_only.sum()) / total, 5) if total else 0.0)
        agreement.append(stat)

        if sar_speaks and opt_speaks:
            recovered = int((sar_only & obscured).sum())
            if recovered > max(64, total * 0.002):
                area = optical.area_ha(recovered)
                complementarity.append(
                    f"SAR detected {CLASS_LABELS.get(name, name)} over "
                    f"{recovered / max(total, 1) * 100:.1f}% of the scene"
                    f"{f' ({area:.1f} ha)' if area else ''} where the optical image is obscured — "
                    "information the optical sensor alone could not provide.")
            if stat.cohen_kappa is not None and stat.cohen_kappa < 0.2 and (
                    (stat.optical_fraction or 0) > 0.02 or (stat.sar_fraction or 0) > 0.02):
                complementarity.append(
                    f"Optical and SAR disagree about {CLASS_LABELS.get(name, name)} "
                    f"(kappa {stat.cohen_kappa:.2f}): reported as low-agreement rather than merged.")

    # Anything not claimed by a joint class.
    claimed = np.zeros_like(valid)
    for m in joint_masks.values():
        claimed |= m
    joint_masks[OTHER] = valid & ~claimed

    joint_classes: List[ClassStat] = []
    regions: List[Region] = []
    min_px = settings.min_region_pixels if min_region_pixels is None else int(min_region_pixels)
    for name, m in joint_masks.items():
        count = int(m.sum())
        joint_classes.append(class_stat(
            name=name, pixel_count=count, total=total, area_ha=optical.area_ha(count),
            basis=provenance.get(name, "residual after joint assignment"),
            reliability=("high" if name in (WATER, BUILT_UP) else "medium"),
            region_count=0))
        if name in (WATER, BUILT_UP) and count > 0:
            regions.extend(label_regions(
                m, class_name=name, min_pixels=min_px, pixel_area_m2=optical.pixel_area_m2,
                north_up=bool(optical.info.crs), limit=regions_per_class, total_valid=total))

    kappas = [s.cohen_kappa for s in agreement if s.cohen_kappa is not None]
    mean_agreement = round(float(np.mean(kappas)), 4) if kappas else None
    if mean_agreement is None:
        notes.append("Cross-modal agreement could not be computed for any class; "
                     "confidence for this answer is reported as unavailable.")

    quality = QualityReport(
        band_layout=f"optical:{optical.bands.layout} + sar:{sar_aligned.bands.layout}",
        bands_available=optical.bands.available() + [f"sar:{r}" for r in sar_aligned.bands.available()],
        indices_available=sorted(set(lc_opt.stack.available) | set(lc_sar.stack.available)),
        indices_unavailable=sorted(set(lc_opt.stack.unavailable) & set(lc_sar.stack.unavailable)),
        valid_pixel_fraction=round(float(valid.mean()), 4),
        separability=lc_opt.measurement.quality.separability,
        spatial_reference=bool(optical.info.crs and sar.info.crs),
        limitations=(lc_opt.measurement.quality.limitations +
                     lc_sar.measurement.quality.limitations + notes))

    measurement = FusionMeasurement(
        method="dual_branch_threshold_agreement_v1",
        joint_classes=sorted(joint_classes, key=lambda c: -c.fraction),
        agreement=agreement, mean_agreement=mean_agreement,
        obscured_fraction=round(float(obscured.sum()) / total, 5) if total else 0.0,
        complementarity=complementarity,
        regions=sorted(regions, key=lambda r: -r.area_px)[:10],
        optical=lc_opt.measurement, sar=lc_sar.measurement, quality=quality)

    preds, source = _scene_labels_dual(optical, lc_opt.stack, sar_aligned, lc_sar.stack)
    if preds and measurement.optical is not None:
        measurement.optical.scene_labels = preds
        measurement.optical.label_source = source

    masks = dict(joint_masks)
    masks["valid"] = valid
    masks["obscured"] = obscured
    for name in _JOINT_CLASSES:
        masks[f"optical_{name}"] = lc_opt.mask(name)
        masks[f"sar_{name}"] = lc_sar.mask(name)
    return measurement, masks


def _scene_labels_dual(optical, optical_stack, sar, sar_stack):
    from backend.analysis import scene_labels

    return scene_labels.predict(optical=optical, optical_stack=optical_stack,
                                sar=sar, sar_stack=sar_stack)


def _sar_can_speak(class_name: str, stack: IndexStack) -> bool:
    if class_name == VEGETATION:
        return stack.has("SAR_RATIO")
    return stack.has("SAR_DB")


def _optical_can_speak(class_name: str, stack: IndexStack) -> bool:
    if class_name == WATER:
        return stack.has("NDWI") or stack.has("MNDWI") or stack.has("BWI")
    if class_name == VEGETATION:
        return stack.has("NDVI") or stack.has("EXG")
    if class_name == BUILT_UP:
        return stack.has("NDBI") or stack.has("BRIGHTNESS")
    return True
