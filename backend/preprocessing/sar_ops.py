"""SAR preprocessing: speckle filtering, dB scaling, texture, per-scene normalization.

CLAUDE.md section 6 is explicit that this preprocessing matters more than model
choice for the cross-modal specialist, because SAR dynamic range varies wildly
between products. RISAT scenes arrive as amplitude or intensity DN, Sentinel-1 /
BigEarthNet-S1 patches arrive already in dB, and re-logging a dB product
destroys it. Everything here therefore *detects* the radiometric convention
before touching the numbers, and reports what it decided.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
from scipy import ndimage

from backend.preprocessing.geotiff_io import percentile_normalize


@dataclass
class SarChannel:
    """One calibrated SAR channel, in decibels, with its provenance."""

    db: np.ndarray                       # (H, W) float32, dB
    convention: str                      # already_db | amplitude | intensity | unitless
    enl: float                           # equivalent number of looks (speckle strength)
    speckle_filtered: bool = False
    calibrated: bool = False             # values sit in a plausible sigma0 range
    notes: List[str] = field(default_factory=list)

    def to_audit(self) -> dict:
        return {"radiometric_convention": self.convention,
                "absolute_calibration_plausible": self.calibrated,
                "equivalent_number_of_looks": round(self.enl, 2),
                "speckle_filtered": self.speckle_filtered,
                "db_range": [round(float(np.nanmin(self.db)), 2), round(float(np.nanmax(self.db)), 2)],
                "notes": list(self.notes)}


def detect_convention(x: np.ndarray) -> str:
    """Classify the radiometric convention of a raw SAR channel."""
    finite = x[np.isfinite(x)]
    if finite.size == 0:
        return "unitless"
    lo, hi = float(np.percentile(finite, 1)), float(np.percentile(finite, 99))
    amin, amax = float(finite.min()), float(finite.max())
    if amin < 0.0 and amax < 60.0:
        # Negative values with a small span: sigma0 already in dB (typical -30..+5).
        return "already_db"
    if amax <= 1.5 and amin >= 0.0:
        # Linear sigma0 in [0,1].
        return "intensity"
    if hi > 4000.0 or (hi > 600.0 and hi / max(lo, 1e-6) > 60.0):
        # Wide, heavy-tailed DN range: power/intensity product.
        return "intensity"
    if amax > 1.5:
        return "amplitude"
    return "unitless"


def to_db(x: np.ndarray, convention: Optional[str] = None) -> tuple[np.ndarray, str]:
    """Convert one raw SAR channel to dB without double-logging dB products."""
    a = np.asarray(x, dtype=np.float64)
    conv = convention or detect_convention(a)
    if conv == "already_db":
        return a.astype(np.float32), conv
    pos = np.clip(a, 1e-6, None)
    if conv == "amplitude":
        return (20.0 * np.log10(pos)).astype(np.float32), conv
    if conv == "intensity":
        return (10.0 * np.log10(pos)).astype(np.float32), conv
    # Unitless / degenerate: keep the values, flag them, let confidence drop.
    return a.astype(np.float32), conv


def estimate_enl(x: np.ndarray, win: int = 7) -> float:
    """Equivalent Number of Looks from the most homogeneous windows of the scene.

    ENL = (mean / std)^2 over homogeneous areas. Low ENL means heavy speckle,
    which is a real, measurable reason to trust a SAR-only answer less.
    """
    a = np.nan_to_num(np.asarray(x, dtype=np.float64))
    if a.size < win * win * 4:
        return 1.0
    mu = ndimage.uniform_filter(a, size=win, mode="nearest")
    sq = ndimage.uniform_filter(a * a, size=win, mode="nearest")
    var = np.maximum(sq - mu * mu, 0.0)
    cv = np.sqrt(var) / np.maximum(np.abs(mu), 1e-6)
    # Homogeneous = lowest-CV decile, where CV reflects speckle rather than structure.
    valid = cv[np.isfinite(cv) & (np.abs(mu) > 1e-6)]
    if valid.size == 0:
        return 1.0
    q = float(np.percentile(valid, 10))
    return float(np.clip(1.0 / max(q * q, 1e-6), 0.5, 100.0))


def refined_lee(a: np.ndarray, win: int = 7, enl: Optional[float] = None) -> np.ndarray:
    """Refined-Lee style adaptive speckle filter (CLAUDE.md section 6).

    Classic Lee shrinks towards the local mean by a factor driven by the ratio of
    local variance to speckle variance. The "refined" part here is directional:
    in windows where the local coefficient of variation exceeds what pure speckle
    can explain (i.e. real structure such as a field boundary or a building
    edge), the filter is applied along the dominant local gradient direction
    instead of isotropically, so edges survive.
    """
    x = np.asarray(a, dtype=np.float64)
    if x.ndim != 2 or x.size < win * win * 4:
        return x.astype(np.float32)
    looks = enl if enl and enl > 0 else estimate_enl(x, win=win)
    cu2 = 1.0 / max(looks, 0.5)                     # speckle variance coefficient

    mu = ndimage.uniform_filter(x, size=win, mode="nearest")
    sq = ndimage.uniform_filter(x * x, size=win, mode="nearest")
    var = np.maximum(sq - mu * mu, 0.0)
    ci2 = var / np.maximum(mu * mu, 1e-9)

    # Isotropic Lee weight.
    w = np.clip((ci2 - cu2) / np.maximum(ci2 * (1.0 + cu2), 1e-9), 0.0, 1.0)
    lee = mu + w * (x - mu)

    # Directional pass for structured windows: average along the edge, not across it.
    gy, gx = np.gradient(ndimage.uniform_filter(x, size=3, mode="nearest"))
    ang = np.arctan2(gy, gx)
    sector = np.mod(np.round(ang / (np.pi / 4.0)).astype(np.int8), 4)  # 4 edge orientations
    directional = np.zeros_like(x)
    kernels = {
        0: np.array([[0, 0, 0], [1, 1, 1], [0, 0, 0]], dtype=np.float64),   # horizontal edge
        1: np.array([[0, 0, 1], [0, 1, 0], [1, 0, 0]], dtype=np.float64),   # anti-diagonal
        2: np.array([[0, 1, 0], [0, 1, 0], [0, 1, 0]], dtype=np.float64),   # vertical edge
        3: np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float64),   # diagonal
    }
    for key, k in kernels.items():
        smoothed = ndimage.convolve(x, k / k.sum(), mode="nearest")
        directional = np.where(sector == key, smoothed, directional)
    structured = ci2 > (4.0 * cu2)
    # In heterogeneous windows use the directional estimate alone. Blending it with
    # the isotropic Lee result would keep half the cross-edge smoothing, which is
    # what smears a narrow river into its banks and loses it entirely.
    out = np.where(structured, directional, lee)
    return out.astype(np.float32)


def local_texture(db: np.ndarray, win: int = 5) -> np.ndarray:
    """Local standard deviation of dB values -- the built-up discriminator.

    Man-made structures produce strong double-bounce returns next to radar
    shadow, so built-up areas are simultaneously bright and *rough* in dB space,
    while smooth water and homogeneous fields are not.
    """
    x = np.nan_to_num(np.asarray(db, dtype=np.float64))
    mu = ndimage.uniform_filter(x, size=win, mode="nearest")
    sq = ndimage.uniform_filter(x * x, size=win, mode="nearest")
    return np.sqrt(np.maximum(sq - mu * mu, 0.0)).astype(np.float32)


def prepare_channel(raw: np.ndarray, speckle_filter: bool = True,
                    convention: Optional[str] = None) -> SarChannel:
    """Raw SAR plane -> calibrated dB channel with speckle handled."""
    notes: List[str] = []
    db, conv = to_db(raw, convention)
    if conv == "unitless":
        notes.append("Radiometric convention could not be determined; values used as-is "
                     "and SAR-derived thresholds treated as relative only.")
    else:
        notes.append(f"Interpreted as {conv}; converted to dB.")
    linear = np.power(10.0, np.clip(db, -60.0, 60.0) / 10.0)
    enl = estimate_enl(linear)
    notes.append(f"Estimated ENL {enl:.1f} from the most homogeneous windows.")
    if speckle_filter:
        db = refined_lee(db, win=7, enl=enl)
        notes.append("Refined-Lee speckle filter applied (7x7, edge-preserving).")

    # A digital-number product carries an unknown gain, so its dB values are offset
    # from true sigma0 by a constant. Differences (and therefore texture) survive,
    # absolute thresholds do not -- so say which case this is.
    finite = db[np.isfinite(db)]
    calibrated = False
    if finite.size:
        lo, hi = float(np.percentile(finite, 1)), float(np.percentile(finite, 99))
        calibrated = -45.0 <= lo and hi <= 12.0
        if not calibrated:
            notes.append(f"Backscatter spans {lo:.1f}..{hi:.1f} dB, outside the calibrated sigma0 "
                         "range: treated as an uncalibrated digital-number product, so level-based "
                         "thresholds are derived from the scene itself rather than from published "
                         "physical values.")
    return SarChannel(db=np.asarray(db, dtype=np.float32), convention=conv, enl=enl,
                      speckle_filtered=bool(speckle_filter), calibrated=calibrated, notes=notes)


def sar_to_display(sar: np.ndarray, speckle_filter: bool = True) -> np.ndarray:
    """Raw SAR product -> pseudo-RGB uint8 for display and for VLM input."""
    a = np.asarray(sar, dtype=np.float64)
    if a.ndim == 3:
        chans = [prepare_channel(p, speckle_filter=speckle_filter).db for p in a]
        if len(chans) >= 2:
            # Dual-pol false colour: co-pol / cross-pol / ratio reads like an RGB scene.
            co, cross = chans[0], chans[1]
            ratio = co - cross
            planes = [percentile_normalize(co), percentile_normalize(cross), percentile_normalize(ratio)]
            return (np.clip(np.stack(planes, axis=-1), 0.0, 1.0) * 255).astype(np.uint8)
        a = chans[0]
    else:
        a = prepare_channel(a, speckle_filter=speckle_filter).db
    norm = percentile_normalize(a, 2.0, 98.0)
    return (np.clip(np.stack([norm] * 3, axis=-1), 0.0, 1.0) * 255).astype(np.uint8)


# Backwards-compatible alias: earlier revisions exposed a plain Lee filter.
def lee_filter(a: np.ndarray, win: int = 3, rms: float = 0.25) -> np.ndarray:
    return refined_lee(a, win=max(3, win))
