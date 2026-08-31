"""Thresholding and separability.

Every land-cover decision in this system reduces to "split this index at a
threshold", so the threshold and the *quality* of the split are recorded rather
than hidden. Otsu's between-class variance ratio (eta) is a genuine, bounded
measure of how bimodal the evidence was, and it is the primary honest confidence
signal for the analysis path -- a scene where water and land separate cleanly
earns a high number, a flat featureless chip does not.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np


@dataclass
class Threshold:
    value: float
    method: str
    separability: float               # Otsu eta in [0, 1]
    fraction_above: float
    note: str = ""

    def to_audit(self) -> dict:
        return {"value": round(self.value, 4), "method": self.method,
                "separability": round(self.separability, 4),
                "fraction_above": round(self.fraction_above, 4), "note": self.note}


def _finite(values: np.ndarray) -> np.ndarray:
    v = np.asarray(values, dtype=np.float64).ravel()
    return v[np.isfinite(v)]


def otsu(values: np.ndarray, bins: int = 256,
         lo: Optional[float] = None, hi: Optional[float] = None) -> Tuple[float, float]:
    """Otsu threshold and its between-class variance ratio.

    ``eta = sigma_between^2 / sigma_total^2`` in [0, 1]: 1 means two perfectly
    separated modes, values below ~0.3 mean the histogram is effectively
    unimodal and any threshold on it is arbitrary.
    """
    v = _finite(values)
    if v.size < 32:
        return float(np.median(v)) if v.size else 0.0, 0.0
    vmin = float(np.percentile(v, 0.5)) if lo is None else lo
    vmax = float(np.percentile(v, 99.5)) if hi is None else hi
    if vmax <= vmin:
        return float(vmin), 0.0
    clipped = np.clip(v, vmin, vmax)
    hist, edges = np.histogram(clipped, bins=bins, range=(vmin, vmax))
    hist = hist.astype(np.float64)
    total = hist.sum()
    if total <= 0:
        return float(vmin), 0.0
    p = hist / total
    centres = (edges[:-1] + edges[1:]) / 2.0
    omega = np.cumsum(p)
    mu = np.cumsum(p * centres)
    mu_t = mu[-1]
    denom = omega * (1.0 - omega)
    with np.errstate(divide="ignore", invalid="ignore"):
        sigma_b = np.where(denom > 1e-12, (mu_t * omega - mu) ** 2 / np.maximum(denom, 1e-12), 0.0)
    k = int(np.nanargmax(sigma_b))
    sigma_total = float(np.sum(p * (centres - mu_t) ** 2))
    eta = float(sigma_b[k] / sigma_total) if sigma_total > 1e-12 else 0.0
    return float(centres[k]), float(np.clip(eta, 0.0, 1.0))


def fisher_ratio(values: np.ndarray, mask: np.ndarray) -> float:
    """Normalised Fisher criterion between the masked and unmasked populations."""
    v = np.asarray(values, dtype=np.float64)
    m = np.asarray(mask, dtype=bool)
    a, b = v[m & np.isfinite(v)], v[(~m) & np.isfinite(v)]
    if a.size < 16 or b.size < 16:
        return 0.0
    num = (a.mean() - b.mean()) ** 2
    den = a.var() + b.var()
    if den <= 1e-12:
        return 1.0 if num > 0 else 0.0
    return float(np.clip(num / (num + den), 0.0, 1.0))


def threshold_with_prior(values: np.ndarray, prior: float, window: float,
                         name: str = "", min_separability: float = 0.25,
                         lo: Optional[float] = None, hi: Optional[float] = None,
                         dark_mode: bool = False) -> Threshold:
    """Data-driven threshold, accepted only inside a physically plausible window.

    Pure Otsu on a scene that contains no water will happily split the water index
    somewhere in the middle and report 40% water. Worse, Otsu finds the *dominant*
    split in the histogram, which in a mostly-vegetated scene is the
    vegetation/everything-else boundary rather than the land/water boundary being
    looked for.

    So the data only gets to move the threshold if it lands inside
    ``prior +/- window``, which is where the class boundary physically is. Outside
    that window the published threshold is used unchanged and the disagreement is
    recorded -- clamping to the edge of the window would silently produce a
    threshold that neither the physics nor the data supports.

    ``dark_mode`` restricts the histogram to values at or below the median first,
    which isolates the darkest mode. That is what makes a SAR water threshold work
    on a scene where water is a small minority of the pixels.
    """
    v = _finite(values)
    if v.size < 32:
        return Threshold(value=prior, method="prior_only", separability=0.0,
                         fraction_above=float((v > prior).mean()) if v.size else 0.0,
                         note=f"too few valid pixels for a data-driven {name} threshold")
    pool = v
    method_prefix = ""
    if dark_mode:
        median = float(np.median(v))
        subset = v[v <= median]
        if subset.size >= 32 and float(subset.max() - subset.min()) > 1e-9:
            pool = subset
            method_prefix = "dark_mode_"
    t_otsu, eta = otsu(pool, lo=lo, hi=hi)
    low, high = prior - window, prior + window
    if eta < min_separability:
        frac = float((v > prior).mean())
        return Threshold(value=prior, method="prior_fixed", separability=eta,
                         fraction_above=frac,
                         note=(f"{name} histogram is close to unimodal (eta={eta:.2f} < "
                               f"{min_separability:.2f}); used the physical threshold {prior:+.2f} "
                               "instead of an arbitrary data split"))
    if low <= t_otsu <= high:
        return Threshold(value=float(t_otsu), method=f"{method_prefix}otsu", separability=eta,
                         fraction_above=float((v > t_otsu).mean()),
                         note=(f"data-driven split inside the plausible {name} window "
                               f"[{low:+.2f}, {high:+.2f}]"))
    return Threshold(
        value=prior, method=f"{method_prefix}otsu_rejected_prior_used", separability=eta,
        fraction_above=float((v > prior).mean()),
        note=(f"Otsu suggested {t_otsu:+.2f}, outside the plausible {name} window "
              f"[{low:+.2f}, {high:+.2f}] — the dominant histogram split is not this class "
              f"boundary, so the physical threshold {prior:+.2f} was used instead"))


def dark_mode_threshold(values: np.ndarray, name: str = "",
                        min_separability: float = 0.2) -> Optional[Threshold]:
    """Split off the darkest mode of a histogram, with no physical prior.

    Used for uncalibrated SAR digital numbers, where the dB values carry an unknown
    gain so no published level applies, but water is still the darkest surface in
    the scene. Returns ``None`` when the dark side of the histogram is not
    bimodal enough for any threshold to be defensible.
    """
    v = _finite(values)
    if v.size < 64:
        return None
    median = float(np.median(v))
    subset = v[v <= median]
    if subset.size < 64 or float(subset.max() - subset.min()) < 1e-9:
        return None
    value, eta = otsu(subset)
    fraction = float((v < value).mean())
    if eta < min_separability or fraction > 0.5:
        return Threshold(value=float(value), method="dark_mode_otsu_rejected",
                         separability=eta, fraction_above=1.0 - fraction,
                         note=(f"{name}: the dark side of the histogram is not clearly bimodal "
                               f"(eta={eta:.2f}, would select {fraction * 100:.0f}% of the scene), "
                               "so no level threshold is claimed"))
    return Threshold(value=float(value), method="dark_mode_otsu", separability=eta,
                     fraction_above=1.0 - fraction,
                     note=(f"{name}: darkest histogram mode split at {value:.2f} "
                           f"(scene-relative; the product carries an unknown gain)"))


def robust_scale(a: np.ndarray, valid: Optional[np.ndarray] = None) -> np.ndarray:
    """Scale to 0..1 using the 2nd/98th percentile of valid pixels."""
    x = np.asarray(a, dtype=np.float64)
    pool = x[valid] if valid is not None else x
    pool = pool[np.isfinite(pool)]
    if pool.size == 0:
        return np.zeros_like(x)
    lo, hi = np.percentile(pool, [2.0, 98.0])
    if hi <= lo:
        hi = lo + 1e-6
    return np.clip((np.nan_to_num(x, nan=lo) - lo) / (hi - lo), 0.0, 1.0)
