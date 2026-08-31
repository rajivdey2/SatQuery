"""Bi-temporal change measurement.

The mandatory change task is answered from measured quantities, not from prose:
per-class area deltas in hectares, a transition matrix, a significance test
against the classifier's own instability, and a change mask that is rendered as
the spatial change map the problem statement allows as an extra output.

Two details do most of the work for reliability:

* **Relative radiometric normalisation.** Two acquisitions of the same place
  differ in sun angle, atmosphere and gain. Without normalising the later scene
  onto the earlier scene's statistics, change detection mostly measures the
  weather.
* **A noise floor.** Change Vector Analysis always produces a non-zero
  magnitude everywhere. The threshold is therefore anchored above an estimated
  noise floor (median + 3 x MAD of the magnitude), and per-class deltas are only
  called "increased"/"decreased" when they exceed the flip rate the classifier
  shows on pixels that did *not* change.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np

from backend.analysis.indices import compute_indices
from backend.analysis.landcover import LandCoverResult, measure_land_cover
from backend.analysis.measurements import (BUILT_UP, CLASS_LABELS, CLASS_ORDER, OTHER,
                                           ChangeMeasurement, ClassDelta, QualityReport)
from backend.analysis.regions import dominant_direction, label_regions, smooth_mask
from backend.analysis.thresholds import otsu
from backend.config import settings
from backend.preprocessing.pipeline import PreparedImage, align_pair

# Indices used for Change Vector Analysis, in preference order.
_CVA_OPTICAL = ("NDVI", "NDWI", "MNDWI", "NDBI", "BRIGHTNESS")
_CVA_SAR = ("SAR_DB", "SAR_TEXTURE")


def _radiometric_match(later: np.ndarray, earlier: np.ndarray,
                       valid: np.ndarray) -> Tuple[np.ndarray, List[str]]:
    """Match the later scene's per-band mean/std to the earlier scene's."""
    notes: List[str] = []
    if later.shape != earlier.shape:
        return later, ["Band stacks differ in shape; radiometric normalisation skipped."]
    out = np.array(later, dtype=np.float32, copy=True)
    shifts: List[str] = []
    for b in range(later.shape[0]):
        a = earlier[b][valid]
        c = later[b][valid]
        a, c = a[np.isfinite(a)], c[np.isfinite(c)]
        if a.size < 64 or c.size < 64:
            continue
        sa, sc = float(a.std()), float(c.std())
        if sc < 1e-9:
            continue
        gain = sa / sc
        offset = float(a.mean()) - gain * float(c.mean())
        out[b] = out[b] * gain + offset
        if abs(gain - 1.0) > 0.02 or abs(offset) > 0.02 * max(abs(float(a.mean())), 1.0):
            shifts.append(f"band{b}: gain {gain:.3f}, offset {offset:+.3f}")
    if shifts:
        notes.append("Relative radiometric normalisation applied to the later acquisition (" +
                     "; ".join(shifts[:4]) + (", ..." if len(shifts) > 4 else "") + ").")
    else:
        notes.append("Acquisitions were already radiometrically comparable; no normalisation needed.")
    return out, notes


def _cva_magnitude(stack1, stack2, valid: np.ndarray,
                   names: Tuple[str, ...]) -> Tuple[Optional[np.ndarray], List[str]]:
    """L2 magnitude of the per-index difference vector."""
    used: List[str] = []
    diffs: List[np.ndarray] = []
    for name in names:
        a, b = stack1.get(name), stack2.get(name)
        if a is None or b is None or a.shape != b.shape:
            continue
        diffs.append(np.nan_to_num(b - a))
        used.append(name)
    if not diffs:
        return None, used
    mag = np.sqrt(np.sum(np.stack(diffs, axis=0) ** 2, axis=0))
    mag[~valid] = 0.0
    return mag, used


def _noise_floor(mag: np.ndarray, valid: np.ndarray) -> float:
    pool = mag[valid]
    pool = pool[np.isfinite(pool)]
    if pool.size < 64:
        return 0.0
    med = float(np.median(pool))
    mad = float(np.median(np.abs(pool - med))) * 1.4826
    return med + 3.0 * mad


def _flip_rate(mask1: np.ndarray, mask2: np.ndarray, stable: np.ndarray) -> float:
    """How often the classifier changes its mind where nothing changed."""
    if stable.sum() < 64:
        return 0.0
    a, b = mask1[stable], mask2[stable]
    return float(np.mean(a != b))


def measure_change(image_t1: PreparedImage, image_t2: PreparedImage,
                   min_region_pixels: Optional[int] = None,
                   smoothing_window: Optional[int] = None,
                   change_threshold: Optional[float] = None,
                   significance: Optional[float] = None,
                   normalize_radiometry: bool = True) -> Tuple[ChangeMeasurement, Dict[str, np.ndarray]]:
    """Measure change between two co-registered acquisitions.

    Returns the measurement plus the pixel masks (change mask and both label
    maps) so the caller can render overlays without recomputing.
    """
    t2 = align_pair(image_t1, image_t2)
    notes: List[str] = []
    if t2 is not image_t2:
        notes.append(f"Later acquisition resampled onto the earlier grid "
                     f"({image_t2.shape[0]}x{image_t2.shape[1]} -> {t2.shape[0]}x{t2.shape[1]}).")

    valid = image_t1.valid & t2.valid
    if valid.sum() < 64:
        notes.append("Fewer than 64 jointly valid pixels: change measurement is not meaningful.")

    same_layout = (image_t1.bands.layout == t2.bands.layout and
                   image_t1.raw.shape[0] == t2.raw.shape[0] and
                   image_t1.is_sar == t2.is_sar)
    t2_for_analysis = t2
    if normalize_radiometry and same_layout:
        matched, rn_notes = _radiometric_match(t2.raw, image_t1.raw, valid)
        notes.extend(rn_notes)
        t2_for_analysis = PreparedImage(
            file=t2.file, info=t2.info, raw=matched, bands=t2.bands, rgb=t2.rgb,
            valid=t2.valid, sar_channels=t2.sar_channels, preview_path=t2.preview_path,
            read_scale=t2.read_scale, pixel_area_m2=t2.pixel_area_m2, notes=t2.notes)
    elif not same_layout:
        notes.append("The two acquisitions have different band layouts or modalities: "
                     "radiometric normalisation skipped and change is measured at class level only, "
                     "which is less sensitive than per-band change vector analysis.")

    lc1 = measure_land_cover(image_t1, min_region_pixels=min_region_pixels,
                             smoothing_window=smoothing_window, with_scene_labels=False)
    lc2 = measure_land_cover(t2_for_analysis, min_region_pixels=min_region_pixels,
                             smoothing_window=smoothing_window, with_scene_labels=False)

    stack1 = lc1.stack or compute_indices(image_t1)
    stack2 = lc2.stack or compute_indices(t2_for_analysis)
    cva_names = _CVA_SAR if stack1.basis == "sar" else _CVA_OPTICAL
    mag, used = _cva_magnitude(stack1, stack2, valid, cva_names)

    floor = 0.0
    threshold = None
    if mag is not None:
        floor = _noise_floor(mag, valid)
        t_otsu, eta = otsu(mag[valid], lo=0.0)
        threshold = float(change_threshold) if change_threshold is not None else max(t_otsu, floor)
        method_note = (f"Change Vector Analysis over {', '.join(used)}; threshold "
                       f"{threshold:.4f} (Otsu {t_otsu:.4f}, separability {eta:.2f}, "
                       f"noise floor {floor:.4f})")
        if change_threshold is not None:
            method_note += " [threshold supplied as a permitted task parameter]"
        notes.append(method_note)
        changed = (mag > threshold) & valid
    else:
        notes.append("No comparable index pair for change vector analysis; "
                     "change mask derived from land-cover label disagreement only.")
        changed = (lc1.labels != lc2.labels) & valid

    win = settings.smoothing_window if smoothing_window is None else int(smoothing_window)
    changed = smooth_mask(changed, win=max(2, win))
    changed &= valid
    stable = valid & ~changed

    total = int(valid.sum())
    changed_px = int(changed.sum())
    sig_floor = settings.change_significance if significance is None else float(significance)

    deltas: List[ClassDelta] = []
    for name in CLASS_ORDER:
        if name == OTHER:
            continue
        m1, m2 = lc1.mask(name) & valid, lc2.mask(name) & valid
        f1 = float(m1.sum()) / total if total else 0.0
        f2 = float(m2.sum()) / total if total else 0.0
        d = f2 - f1
        instability = _flip_rate(m1, m2, stable)
        # Significant only if it clears both the caller's floor and twice the
        # classifier's own flip rate on unchanged ground.
        significant = abs(d) > max(sig_floor, 2.0 * instability)
        direction = "unchanged"
        if significant:
            direction = "increased" if d > 0 else "decreased"
        deltas.append(ClassDelta(
            name=name, label=CLASS_LABELS.get(name, name),
            t1_fraction=round(f1, 5), t2_fraction=round(f2, 5),
            delta_fraction=round(d, 5),
            delta_ha=(round(image_t1.area_ha(d * total), 3) if image_t1.pixel_area_m2 else None),
            relative_change=(round(d / f1, 4) if f1 > 1e-6 else None),
            direction=direction, significant=significant))

    transitions = _transition_table(lc1, lc2, changed, valid, image_t1)
    clusters = label_regions(
        changed, class_name="change", min_pixels=(min_region_pixels if min_region_pixels is not None
                                                 else settings.min_region_pixels),
        pixel_area_m2=image_t1.pixel_area_m2, north_up=bool(image_t1.info.crs),
        limit=6, total_valid=total, label="changed area")

    quality = QualityReport(
        band_layout=f"{image_t1.bands.layout} vs {t2.bands.layout}",
        bands_available=sorted(set(image_t1.bands.available()) & set(t2.bands.available())),
        indices_available=used or sorted(set(stack1.available) & set(stack2.available)),
        indices_unavailable=sorted(set(stack1.unavailable) | set(stack2.unavailable)),
        valid_pixel_fraction=round(float(valid.mean()), 4),
        separability=lc1.measurement.quality.separability,
        spatial_reference=bool(image_t1.info.crs),
        limitations=(lc1.measurement.quality.limitations +
                     ([] if same_layout else ["Acquisitions are not radiometrically comparable."]) +
                     ([] if mag is not None else ["Change vector analysis unavailable for this pair."])))

    measurement = ChangeMeasurement(
        method=("cva_" + "_".join(n.lower() for n in used) if used else "label_disagreement"),
        per_class=deltas, transitions=transitions,
        changed_fraction=round(changed_px / total, 5) if total else 0.0,
        changed_area_ha=(round(image_t1.area_ha(changed_px), 3) if image_t1.pixel_area_m2 else None),
        magnitude_threshold=round(threshold, 5) if threshold is not None else None,
        noise_floor=round(floor, 5) if mag is not None else None,
        clusters=clusters,
        t1_date=image_t1.info.acquisition_date, t2_date=t2.info.acquisition_date,
        t1=lc1.measurement, t2=lc2.measurement, quality=quality)
    measurement.quality.limitations.extend(notes)

    masks = {"changed": changed, "valid": valid,
             "labels_t1": lc1.labels, "labels_t2": lc2.labels}
    for name in CLASS_ORDER:
        masks[f"t1_{name}"] = lc1.mask(name)
        masks[f"t2_{name}"] = lc2.mask(name)
    return measurement, masks


def _transition_table(lc1: LandCoverResult, lc2: LandCoverResult, changed: np.ndarray,
                      valid: np.ndarray, ref: PreparedImage) -> List[dict]:
    """From-class -> to-class pixel counts over the changed area."""
    out: List[dict] = []
    total = int(valid.sum()) or 1
    for i, src in enumerate(CLASS_ORDER):
        m1 = lc1.mask(src) & changed
        if not m1.any():
            continue
        for j, dst in enumerate(CLASS_ORDER):
            if src == dst:
                continue
            m = m1 & lc2.mask(dst)
            n = int(m.sum())
            if n <= 0:
                continue
            out.append({"from": src, "to": dst, "pixels": n,
                        "fraction": round(n / total, 5),
                        "area_ha": (round(ref.area_ha(n), 3) if ref.pixel_area_m2 else None),
                        "location": dominant_direction(m, north_up=bool(ref.info.crs))})
    out.sort(key=lambda r: -r["pixels"])
    return out[:8]


def built_up_verdict(measurement: ChangeMeasurement) -> str:
    """Direct answer for the problem statement's built-up trend query."""
    d = measurement.delta(BUILT_UP)
    if d is None:
        return "unchanged"
    return d.direction
