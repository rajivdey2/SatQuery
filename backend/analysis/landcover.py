"""Land-cover measurement for a single image.

Physically-motivated indices, data-driven thresholds anchored to published
priors, morphological cleanup, then per-class extent and connected regions. The
classifier degrades explicitly rather than silently: an RGB benchmark PNG is
classified from visible proxies and every class carries the evidence it rests on
and a reliability grade, so the confidence stage and the answer text can both be
honest about it.

Where the adapted BigEarthNet-MM head (``training/adapt_ben_mm.py``) is present,
its multi-label scene predictions are attached alongside -- that is the
remote-sensing-adapted visual component required by the problem statement, and
its class probabilities are a real (learned) confidence signal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from backend.analysis import scene_labels
from backend.analysis.indices import (DARK_MODE_INDICES, INDEX_PRIORS, IndexStack,
                                      compute_indices, index_limitations)
from backend.analysis.measurements import (BARE_SOIL, BUILT_UP, CLASS_LABELS, CLASS_ORDER,
                                           OTHER, VEGETATION, WATER, ClassStat,
                                           LandCoverMeasurement, QualityReport, Region,
                                           class_stat)
from backend.analysis.regions import label_regions, region_count, smooth_mask
from backend.analysis.thresholds import Threshold, threshold_with_prior
from backend.config import settings
from backend.preprocessing.bands import ROLE_NIR
from backend.preprocessing.pipeline import PreparedImage


@dataclass
class LandCoverResult:
    """Pixel-level result plus the audit-ready measurement record."""

    masks: Dict[str, np.ndarray] = field(default_factory=dict)
    labels: Optional[np.ndarray] = None          # (H, W) uint8 index into CLASS_ORDER
    measurement: LandCoverMeasurement = field(default_factory=LandCoverMeasurement)
    stack: Optional[IndexStack] = None
    thresholds: Dict[str, Threshold] = field(default_factory=dict)

    def mask(self, class_name: str) -> np.ndarray:
        if class_name in self.masks:
            return self.masks[class_name]
        shape = self.labels.shape if self.labels is not None else (1, 1)
        return np.zeros(shape, dtype=bool)


def _apply(stack: IndexStack, name: str, thresholds: Dict[str, Threshold],
           prior_override: Optional[tuple] = None) -> Optional[Threshold]:
    """Threshold one index once and memoise it for the audit trace."""
    if name in thresholds:
        return thresholds[name]
    arr = stack.get(name)
    if arr is None:
        return None
    prior, window = prior_override or INDEX_PRIORS.get(name, (0.0, 0.25))
    pool = arr[stack.valid] if stack.valid is not None else arr
    t = threshold_with_prior(pool, prior=prior, window=window, name=name,
                             dark_mode=name in DARK_MODE_INDICES)
    thresholds[name] = t
    stack.thresholds[name] = t
    return t


def _percentile(arr: np.ndarray, valid: Optional[np.ndarray], q: float) -> float:
    pool = arr[valid] if valid is not None else arr
    pool = pool[np.isfinite(pool)]
    return float(np.percentile(pool, q)) if pool.size else 0.0


def _sar_level_threshold(db: np.ndarray, valid: Optional[np.ndarray], calibrated: bool,
                         thresholds: Dict[str, Threshold],
                         stack: IndexStack) -> Optional[Threshold]:
    """Water threshold on the backscatter level.

    For a calibrated sigma0 product the published physical prior applies (open water
    sits near -16 dB), with the data allowed to move it inside that window. A
    digital-number product carries an unknown gain, so the same absolute number is
    meaningless; the split is then taken from the darkest mode of the scene's own
    histogram, and only when that mode is genuinely separable.
    """
    from backend.analysis.thresholds import dark_mode_threshold

    pool = db[valid] if valid is not None else db
    if calibrated:
        t = _apply(stack, "SAR_DB", thresholds)
        if t is None or t.separability < 0.2:
            return t
        return t
    t = dark_mode_threshold(pool, name="backscatter")
    if t is None:
        return None
    thresholds["SAR_DB"] = t
    stack.thresholds["SAR_DB"] = t
    return None if t.method.endswith("rejected") else t


def _classify_multispectral(img: PreparedImage, stack: IndexStack,
                            thresholds: Dict[str, Threshold]) -> tuple[Dict[str, np.ndarray], Dict[str, str]]:
    valid = stack.valid
    basis: Dict[str, str] = {}
    masks: Dict[str, np.ndarray] = {}

    water_index = "MNDWI" if stack.has("MNDWI") else "NDWI"
    t_water = _apply(stack, water_index, thresholds)
    water = np.zeros(img.shape, dtype=bool)
    if t_water is not None:
        water = stack.get(water_index) > t_water.value
        nir = img.band(ROLE_NIR)
        if nir is not None:
            # Open water is dark in the near-infrared: this guard removes the
            # classic false positive over dark asphalt and building shadow.
            water &= nir <= _percentile(nir, valid, 55)
        basis[WATER] = f"{water_index} > {t_water.value:+.3f} ({t_water.method}) with NIR-darkness guard"
    else:
        basis[WATER] = ("no water claimed: this product carries neither a green+NIR nor a "
                        "green+SWIR pair, so no water index can be computed")

    t_veg = _apply(stack, "NDVI", thresholds)
    vegetation = np.zeros(img.shape, dtype=bool)
    if t_veg is not None:
        vegetation = (stack.get("NDVI") > t_veg.value) & ~water
        basis[VEGETATION] = f"NDVI > {t_veg.value:+.3f} ({t_veg.method})"

    built = np.zeros(img.shape, dtype=bool)
    if stack.has("NDBI"):
        t_built = _apply(stack, "NDBI", thresholds)
        built = (stack.get("NDBI") > t_built.value) & ~water & ~vegetation
        basis[BUILT_UP] = f"NDBI > {t_built.value:+.3f} ({t_built.method})"
    else:
        bright = stack.get("BRIGHTNESS")
        tex = stack.get("TEXTURE")
        b_hi = _percentile(bright, valid, 60)
        t_hi = _percentile(tex, valid, 70)
        built = (bright > b_hi) & (tex > t_hi) & ~water & ~vegetation
        basis[BUILT_UP] = (f"no SWIR band: brightness > p60 ({b_hi:.3f}) and 5x5 texture > p70 "
                          f"({t_hi:.3f}), outside water and vegetation")

    assigned = water | vegetation | built
    tex = stack.get("TEXTURE")
    t_smooth = _percentile(tex, valid, 60)
    bare = (~assigned) & (tex <= t_smooth)
    basis[BARE_SOIL] = f"unvegetated, non-water residue with 5x5 texture <= p60 ({t_smooth:.3f})"

    masks[WATER], masks[VEGETATION], masks[BUILT_UP], masks[BARE_SOIL] = water, vegetation, built, bare
    masks[OTHER] = ~(water | vegetation | built | bare)
    basis[OTHER] = "no class threshold satisfied"
    return masks, basis


def _classify_visible_only(img: PreparedImage, stack: IndexStack,
                           thresholds: Dict[str, Threshold]) -> tuple[Dict[str, np.ndarray], Dict[str, str]]:
    valid = stack.valid
    basis: Dict[str, str] = {}
    bright, tex = stack.get("BRIGHTNESS"), stack.get("TEXTURE")

    t_water = _apply(stack, "BWI", thresholds)
    dark = bright <= _percentile(bright, valid, 45)
    water = (stack.get("BWI") > t_water.value) & dark if t_water else np.zeros(img.shape, dtype=bool)
    if t_water:
        basis[WATER] = (f"visible-only proxy: (Blue-Red)/(Blue+Red) > {t_water.value:+.3f} "
                        f"({t_water.method}) and brightness below p45")

    t_veg = _apply(stack, "EXG", thresholds)
    vegetation = (stack.get("EXG") > t_veg.value) & ~water if t_veg else np.zeros(img.shape, dtype=bool)
    if t_veg:
        basis[VEGETATION] = f"visible-only proxy: excess-green > {t_veg.value:+.3f} ({t_veg.method})"

    b_hi, t_hi = _percentile(bright, valid, 60), _percentile(tex, valid, 65)
    built = (bright > b_hi) & (tex > t_hi) & ~water & ~vegetation
    basis[BUILT_UP] = f"brightness > p60 ({b_hi:.3f}) with high 5x5 texture > p65 ({t_hi:.3f})"

    assigned = water | vegetation | built
    bare = (~assigned) & (tex <= _percentile(tex, valid, 55))
    basis[BARE_SOIL] = "smooth, non-green, non-water residue"
    masks = {WATER: water, VEGETATION: vegetation, BUILT_UP: built, BARE_SOIL: bare}
    masks[OTHER] = ~(water | vegetation | built | bare)
    basis[OTHER] = "no proxy threshold satisfied"
    return masks, basis


def _classify_panchromatic(img: PreparedImage, stack: IndexStack,
                           thresholds: Dict[str, Threshold]) -> tuple[Dict[str, np.ndarray], Dict[str, str]]:
    valid = stack.valid
    bright, tex = stack.get("BRIGHTNESS"), stack.get("TEXTURE")
    lo, hi = _percentile(bright, valid, 15), _percentile(bright, valid, 80)
    t_smooth, t_rough = _percentile(tex, valid, 40), _percentile(tex, valid, 75)
    water = (bright <= lo) & (tex <= t_smooth)
    built = (bright >= hi) & (tex >= t_rough)
    bare = (~water) & (~built) & (tex <= t_smooth)
    masks = {WATER: water, VEGETATION: np.zeros(img.shape, dtype=bool),
             BUILT_UP: built, BARE_SOIL: bare}
    masks[OTHER] = ~(water | built | bare)
    basis = {
        WATER: f"panchromatic only: dark (<= p15 = {lo:.3f}) and smooth surface, consistent with water",
        VEGETATION: "not separable from a single panchromatic band",
        BUILT_UP: f"panchromatic only: bright (>= p80 = {hi:.3f}) and rough (texture >= p75)",
        BARE_SOIL: "mid-brightness smooth residue",
        OTHER: "mid-brightness textured residue",
    }
    return masks, basis


def _classify_sar(img: PreparedImage, stack: IndexStack,
                  thresholds: Dict[str, Threshold]) -> tuple[Dict[str, np.ndarray], Dict[str, str]]:
    valid = stack.valid
    db, tex = stack.get("SAR_DB"), stack.get("SAR_TEXTURE")
    basis: Dict[str, str] = {}
    if db is None:
        empty = np.zeros(img.shape, dtype=bool)
        return {c: empty.copy() for c in CLASS_ORDER}, {c: "no calibrated SAR channel" for c in CLASS_ORDER}

    calibrated = any(ch.calibrated for ch in img.sar_channels.values()) if img.sar_channels else False
    t_water = _sar_level_threshold(db, valid, calibrated, thresholds, stack)
    smooth_cut = _percentile(tex, valid, 45)
    if t_water is None:
        water = np.zeros(img.shape, dtype=bool)
        basis[WATER] = ("no water claimed: the backscatter histogram is effectively unimodal, so any "
                        "level threshold would be arbitrary on this uncalibrated product")
    else:
        # Open water is a specular reflector: very low backscatter and radiometrically smooth.
        water = (db < t_water.value) & (tex <= smooth_cut)
        scale = "calibrated sigma0" if calibrated else "scene-relative (uncalibrated DN)"
        basis[WATER] = (f"backscatter < {t_water.value:.1f} dB ({t_water.method}, {scale}) with low "
                        f"roughness (texture <= p45 = {smooth_cut:.2f} dB): specular water surface")

    t_rough = _apply(stack, "SAR_TEXTURE", thresholds)
    db_hi = _percentile(db, valid, 75)
    built = (db > db_hi) & (tex > t_rough.value)
    basis[BUILT_UP] = (f"backscatter > p75 ({db_hi:.1f} dB) with roughness > {t_rough.value:.2f} dB: "
                       "double-bounce returns adjacent to radar shadow")

    vegetation = np.zeros(img.shape, dtype=bool)
    if stack.has("SAR_RATIO"):
        ratio = stack.get("SAR_RATIO")
        r_lo = _percentile(ratio, valid, 40)
        vegetation = (~water) & (~built) & (ratio <= r_lo)
        basis[VEGETATION] = (f"co-pol minus cross-pol <= p40 ({r_lo:.1f} dB): volume scattering "
                             "consistent with a vegetation canopy")
    else:
        basis[VEGETATION] = ("not claimed: single-polarisation SAR carries no volume-scattering "
                             "discriminator for vegetation")

    bare = (~water) & (~built) & (~vegetation) & (tex <= smooth_cut)
    basis[BARE_SOIL] = "low-roughness surface residue, consistent with bare or fallow ground"
    masks = {WATER: water, VEGETATION: vegetation, BUILT_UP: built, BARE_SOIL: bare}
    masks[OTHER] = ~(water | vegetation | built | bare)
    basis[OTHER] = "intermediate backscatter and roughness, not assigned"
    return masks, basis


def _modal_smooth(masks: Dict[str, np.ndarray], valid: np.ndarray, win: int) -> Dict[str, np.ndarray]:
    """Resolve overlaps and remove salt-and-pepper by local majority vote."""
    from scipy import ndimage

    order = [c for c in CLASS_ORDER if c in masks]
    if win >= 2:
        votes = np.stack([ndimage.uniform_filter(masks[c].astype(np.float32), size=win, mode="nearest")
                          for c in order], axis=0)
    else:
        votes = np.stack([masks[c].astype(np.float32) for c in order], axis=0)
    # Keep original hard assignments as a tie-break bonus so the vote smooths
    # boundaries without erasing small genuine regions.
    votes += 0.25 * np.stack([masks[c].astype(np.float32) for c in order], axis=0)
    winner = np.argmax(votes, axis=0)
    out: Dict[str, np.ndarray] = {}
    for i, c in enumerate(order):
        m = (winner == i) & valid
        out[c] = smooth_mask(m, win=win) if win >= 2 else m
    # Smoothing can leave pixels unclaimed; sweep them into OTHER.
    claimed = np.zeros_like(valid)
    for c in order:
        claimed |= out[c]
    out[OTHER] = out.get(OTHER, np.zeros_like(valid)) | (valid & ~claimed)
    for c in order:
        if c != OTHER:
            out[OTHER] &= ~out[c]
    return out


def measure_land_cover(img: PreparedImage, stack: Optional[IndexStack] = None,
                       min_region_pixels: Optional[int] = None,
                       smoothing_window: Optional[int] = None,
                       regions_per_class: int = 4,
                       with_scene_labels: bool = True) -> LandCoverResult:
    """Classify one image and produce its audit-ready measurement."""
    stack = stack or compute_indices(img)
    valid = img.valid if img.valid is not None else np.ones(img.shape, dtype=bool)
    thresholds: Dict[str, Threshold] = {}

    if stack.basis == "sar":
        masks, basis = _classify_sar(img, stack, thresholds)
        method = "sar_backscatter_roughness_v1"
    elif stack.basis == "multispectral":
        masks, basis = _classify_multispectral(img, stack, thresholds)
        method = "spectral_index_thresholds_v1"
    elif stack.basis == "visible_only":
        masks, basis = _classify_visible_only(img, stack, thresholds)
        method = "visible_proxy_thresholds_v1"
    else:
        masks, basis = _classify_panchromatic(img, stack, thresholds)
        method = "brightness_texture_v1"

    masks = {c: (m & valid) for c, m in masks.items()}
    win = settings.smoothing_window if smoothing_window is None else int(smoothing_window)
    masks = _modal_smooth(masks, valid, win)

    total = int(valid.sum())
    min_px = settings.min_region_pixels if min_region_pixels is None else int(min_region_pixels)
    north_up = bool(img.info.crs)
    classes: List[ClassStat] = []
    regions: List[Region] = []
    reliability = {"multispectral": "high", "sar": "medium",
                   "visible_only": "medium", "panchromatic": "low"}[stack.basis]

    for name in CLASS_ORDER:
        m = masks.get(name)
        if m is None:
            continue
        count = int(m.sum())
        rel = reliability
        if name == VEGETATION and stack.basis in ("panchromatic",):
            rel = "unavailable"
        if name == VEGETATION and stack.basis == "sar" and not stack.has("SAR_RATIO"):
            rel = "unavailable"
        classes.append(class_stat(
            name=name, pixel_count=count, total=total, area_ha=img.area_ha(count),
            basis=basis.get(name, ""), reliability=rel,
            region_count=region_count(m, min_pixels=min_px)))
        if name != OTHER and count > 0:
            regions.extend(label_regions(
                m, class_name=name, min_pixels=min_px, pixel_area_m2=img.pixel_area_m2,
                north_up=north_up, limit=regions_per_class, total_valid=total))

    ranked = sorted((c for c in classes if c.name != OTHER), key=lambda c: -c.fraction)
    dominant = ranked[0].name if ranked and ranked[0].fraction > 0 else OTHER

    used = [t.separability for t in thresholds.values()]
    quality = QualityReport(
        band_layout=img.bands.layout,
        bands_available=img.bands.available(),
        indices_available=stack.available,
        indices_unavailable=sorted(stack.unavailable),
        valid_pixel_fraction=round(float(valid.mean()), 4),
        separability=round(float(np.mean(used)), 4) if used else None,
        spatial_reference=bool(img.info.crs),
        limitations=index_limitations(stack) + (
            [] if img.info.crs else ["No CRS/geotransform: areas cannot be reported in hectares and "
                                     "spatial descriptions use image orientation, not compass bearings."]) + (
            ["Band roles for this product were resolved heuristically and may be wrong; "
             "supply explicit band roles to remove the ambiguity."] if img.bands.ambiguous else []))

    measurement = LandCoverMeasurement(
        method=method, classes=classes, dominant=dominant, indices=stack.stats(),
        regions=sorted(regions, key=lambda r: -r.area_px)[:12], quality=quality)

    if with_scene_labels:
        if img.is_sar:
            preds, source = scene_labels.predict(sar=img, sar_stack=stack)
        else:
            preds, source = scene_labels.predict(optical=img, optical_stack=stack)
        if preds:
            measurement.scene_labels = preds
            measurement.label_source = source

    return LandCoverResult(masks=masks, labels=_label_array(masks, valid),
                           measurement=measurement, stack=stack, thresholds=thresholds)


def _label_array(masks: Dict[str, np.ndarray], valid: np.ndarray) -> np.ndarray:
    out = np.full(valid.shape, len(CLASS_ORDER), dtype=np.uint8)   # sentinel = invalid
    for i, name in enumerate(CLASS_ORDER):
        m = masks.get(name)
        if m is not None:
            out[m] = i
    out[~valid] = len(CLASS_ORDER)
    return out


def class_label(name: str) -> str:
    return CLASS_LABELS.get(name, name)
