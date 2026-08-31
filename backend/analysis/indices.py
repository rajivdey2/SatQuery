"""Spectral and radar index computation.

Which indices exist depends entirely on which bands the product carries, and the
system is explicit about that instead of substituting a silent fallback: a
3-band benchmark PNG has no near-infrared, so NDVI is reported as *unavailable*
and a visible-only proxy is used with reduced reliability. That distinction is
what stops the confidence estimate from being fiction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
from scipy import ndimage

from backend.analysis.measurements import IndexStat
from backend.analysis.thresholds import Threshold, robust_scale
from backend.preprocessing.bands import (ROLE_BLUE, ROLE_GREEN, ROLE_NIR, ROLE_PAN,
                                         ROLE_RED, ROLE_SWIR1, ROLE_SWIR2)
from backend.preprocessing.pipeline import PreparedImage

# Physical priors for each index threshold, with the window the data may move it in.
INDEX_PRIORS = {
    "NDWI": (0.0, 0.25),      # McFeeters 1996: open water above 0
    "MNDWI": (0.0, 0.30),     # Xu 2006: more robust to built-up than NDWI
    "NDVI": (0.25, 0.18),     # healthy vegetation clearly above 0.3
    "NDBI": (0.0, 0.10),      # Zha 2003: built-up above 0; bare soil sits just below it,
                              # so the window is deliberately tight
    "EXG": (0.02, 0.10),      # visible-only vegetation proxy
    "BWI": (0.02, 0.12),      # visible-only blue-over-red water proxy
    "SAR_DB": (-16.0, 6.0),   # smooth open water backscatter is very low
    "SAR_TEXTURE": (1.6, 1.2),
}

#: Indices whose class boundary sits on the dark side of the histogram, where the
#: dominant Otsu split would be the wrong boundary.
DARK_MODE_INDICES = {"SAR_DB"}

INDEX_DESCRIPTIONS = {
    "NDVI": "(NIR - Red)/(NIR + Red), vegetation vigour",
    "NDWI": "(Green - NIR)/(Green + NIR), open water",
    "MNDWI": "(Green - SWIR1)/(Green + SWIR1), water robust to built-up",
    "NDBI": "(SWIR1 - NIR)/(SWIR1 + NIR), built-up / impervious surface",
    "EXG": "(2G - R - B)/(R + G + B), visible-only vegetation proxy",
    "BWI": "(Blue - Red)/(Blue + Red), visible-only water proxy",
    "BRIGHTNESS": "mean scaled reflectance across available bands",
    "TEXTURE": "local standard deviation of brightness, 5x5 window",
    "SAR_DB": "calibrated backscatter in dB",
    "SAR_TEXTURE": "local standard deviation of backscatter in dB, 5x5 window",
    "SAR_RATIO": "co-pol minus cross-pol backscatter in dB",
}


@dataclass
class IndexStack:
    """All indices computable for one image, plus why the rest were not."""

    values: Dict[str, np.ndarray] = field(default_factory=dict)
    unavailable: Dict[str, str] = field(default_factory=dict)
    basis: str = "unknown"            # multispectral | visible_only | panchromatic | sar
    valid: Optional[np.ndarray] = None
    thresholds: Dict[str, Threshold] = field(default_factory=dict)

    @property
    def available(self) -> List[str]:
        return sorted(self.values)

    def has(self, *names: str) -> bool:
        return all(n in self.values for n in names)

    def get(self, name: str) -> Optional[np.ndarray]:
        return self.values.get(name)

    def stat(self, name: str) -> IndexStat:
        if name not in self.values:
            return IndexStat(name=name, available=False,
                             note=self.unavailable.get(name, "not computed"))
        v = self.values[name]
        pool = v[self.valid] if self.valid is not None else v
        pool = pool[np.isfinite(pool)]
        t = self.thresholds.get(name)
        if pool.size == 0:
            return IndexStat(name=name, available=False, note="no valid pixels")
        return IndexStat(
            name=name, available=True,
            mean=round(float(pool.mean()), 4), std=round(float(pool.std()), 4),
            p05=round(float(np.percentile(pool, 5)), 4),
            p95=round(float(np.percentile(pool, 95)), 4),
            threshold=round(t.value, 4) if t else None,
            threshold_method=t.method if t else None,
            separability=round(t.separability, 4) if t else None,
            note=(t.note if t and t.note else INDEX_DESCRIPTIONS.get(name, "")))

    def stats(self) -> List[IndexStat]:
        return ([self.stat(n) for n in self.available] +
                [IndexStat(name=n, available=False, note=why)
                 for n, why in sorted(self.unavailable.items())])


def normalized_difference(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """(a - b) / (a + b), NaN-safe and clipped to the valid [-1, 1] range."""
    if a is None or b is None:
        return None
    x, y = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    denom = x + y
    with np.errstate(divide="ignore", invalid="ignore"):
        nd = np.where(np.abs(denom) > 1e-9, (x - y) / np.where(np.abs(denom) > 1e-9, denom, 1.0), 0.0)
    return np.clip(np.nan_to_num(nd, nan=0.0), -1.0, 1.0)


def local_std(a: np.ndarray, win: int = 5) -> np.ndarray:
    x = np.nan_to_num(np.asarray(a, dtype=np.float64))
    mu = ndimage.uniform_filter(x, size=win, mode="nearest")
    sq = ndimage.uniform_filter(x * x, size=win, mode="nearest")
    return np.sqrt(np.maximum(sq - mu * mu, 0.0))


def compute_indices(img: PreparedImage) -> IndexStack:
    """Compute every index the product's bands support."""
    if img.is_sar:
        return _sar_indices(img)
    return _optical_indices(img)


def _optical_indices(img: PreparedImage) -> IndexStack:
    valid = img.valid
    blue, green = img.band(ROLE_BLUE), img.band(ROLE_GREEN)
    red, nir = img.band(ROLE_RED), img.band(ROLE_NIR)
    swir1, swir2 = img.band(ROLE_SWIR1), img.band(ROLE_SWIR2)
    pan = img.band(ROLE_PAN)

    values: Dict[str, np.ndarray] = {}
    unavailable: Dict[str, str] = {}

    if nir is not None and red is not None:
        values["NDVI"] = normalized_difference(nir, red)
    else:
        unavailable["NDVI"] = "requires Red and NIR bands; product has " + ", ".join(img.bands.available())
    if green is not None and nir is not None:
        values["NDWI"] = normalized_difference(green, nir)
    else:
        unavailable["NDWI"] = "requires Green and NIR bands"
    if green is not None and swir1 is not None:
        values["MNDWI"] = normalized_difference(green, swir1)
    else:
        unavailable["MNDWI"] = "requires Green and SWIR1 bands (absent from 4-band and RGB products)"
    if swir1 is not None and nir is not None:
        values["NDBI"] = normalized_difference(swir1, nir)
    else:
        unavailable["NDBI"] = "requires SWIR1 and NIR bands; built-up detected from brightness and texture instead"

    visible = [b for b in (red, green, blue) if b is not None]
    if len(visible) == 3 and "NDVI" not in values:
        r, g, b = [robust_scale(x, valid) for x in (red, green, blue)]
        total = r + g + b
        with np.errstate(divide="ignore", invalid="ignore"):
            values["EXG"] = np.clip(np.where(total > 1e-6, (2 * g - r - b) / np.maximum(total, 1e-6), 0.0), -1, 1)
        values["BWI"] = normalized_difference(b, r)

    stack_for_brightness = [x for x in (blue, green, red, nir, swir1, swir2, pan) if x is not None]
    if stack_for_brightness:
        brightness = np.mean([robust_scale(x, valid) for x in stack_for_brightness], axis=0)
    else:
        brightness = np.zeros(img.shape, dtype=np.float64)
    values["BRIGHTNESS"] = brightness
    values["TEXTURE"] = local_std(brightness, win=5)

    if "NDVI" in values:
        basis = "multispectral"
    elif "EXG" in values:
        basis = "visible_only"
    else:
        basis = "panchromatic"
    return IndexStack(values=values, unavailable=unavailable, basis=basis, valid=valid)


def _sar_indices(img: PreparedImage) -> IndexStack:
    valid = img.valid
    values: Dict[str, np.ndarray] = {}
    unavailable: Dict[str, str] = {}
    db = img.primary_sar_db()
    if db is None:
        unavailable["SAR_DB"] = "no SAR channel could be calibrated"
        return IndexStack(values=values, unavailable=unavailable, basis="sar", valid=valid)
    values["SAR_DB"] = np.asarray(db, dtype=np.float64)
    values["SAR_TEXTURE"] = local_std(values["SAR_DB"], win=5)
    values["BRIGHTNESS"] = robust_scale(values["SAR_DB"], valid)
    values["TEXTURE"] = local_std(values["BRIGHTNESS"], win=5)

    co = next((img.sar_channels[r].db for r in ("vv", "hh") if r in img.sar_channels), None)
    cross = next((img.sar_channels[r].db for r in ("vh", "hv") if r in img.sar_channels), None)
    if co is not None and cross is not None:
        values["SAR_RATIO"] = np.asarray(co, dtype=np.float64) - np.asarray(cross, dtype=np.float64)
    else:
        unavailable["SAR_RATIO"] = ("requires dual-polarisation data (VV+VH or HH+HV); "
                                    "single-pol product carries no polarimetric ratio")
    for name in ("NDVI", "NDWI", "MNDWI", "NDBI"):
        unavailable[name] = "not defined for SAR imagery (no spectral bands)"
    return IndexStack(values=values, unavailable=unavailable, basis="sar", valid=valid)


def index_limitations(stack: IndexStack) -> List[str]:
    """Plain-language limitations, copied into the audit trace and the answer."""
    out: List[str] = []
    if stack.basis == "visible_only":
        out.append("No near-infrared band: NDVI/NDWI unavailable, vegetation and water detected "
                   "from visible-band proxies (ExG, blue-over-red) with reduced reliability.")
    if stack.basis == "panchromatic":
        out.append("Single panchromatic band: land-cover classes are inferred from brightness and "
                   "texture alone and must be treated as indicative, not spectral evidence.")
    if stack.basis == "sar":
        out.append("SAR-only input: classes derive from backscatter level and roughness; "
                   "vegetation vigour and water chemistry are not observable.")
    if stack.basis == "multispectral" and "MNDWI" not in stack.values:
        out.append("No SWIR band: water uses NDWI (more sensitive to built-up confusion than MNDWI) "
                   "and built-up uses brightness+texture rather than NDBI.")
    return out
