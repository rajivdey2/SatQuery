"""Sensor-aware band-role resolution.

The analysis engine needs to know *which* array plane is red, NIR, SWIR, VV, ...
before it can compute a single index. Guessing wrong silently produces a
confident-looking NDVI that is meaningless -- exactly the failure mode the
problem statement's ISRO/SAC evaluation set will expose (Cartosat-2S MX band
order is not Sentinel-2 band order).

Resolution order, most trustworthy first:
  1. Per-band descriptions / band-name tags written by the product generator
     (``rasterio`` ``descriptions``, ``BANDNAMES``, ``band_N_name`` tags).
  2. Band count conventions for the sensors named in the problem statement and
     in CLAUDE.md section 3 (Sentinel-2 / BigEarthNet stacks, Cartosat-2S MX,
     RISAT single-pol, Sentinel-1 dual-pol).
  3. A statistical tie-break for the one genuinely ambiguous case (BGRN vs RGBN
     four-band products), using the vegetation signature: red is the visible
     band most strongly anti-correlated with NIR.

Every resolution records how it was reached in ``BandMap.source`` so the audit
trace can show it and a reviewer can challenge it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

# Canonical roles used by everything downstream.
ROLE_BLUE = "blue"
ROLE_GREEN = "green"
ROLE_RED = "red"
ROLE_NIR = "nir"
ROLE_SWIR1 = "swir1"
ROLE_SWIR2 = "swir2"
ROLE_REDEDGE = "rededge"
ROLE_PAN = "pan"
ROLE_VV = "vv"
ROLE_VH = "vh"
ROLE_HH = "hh"
ROLE_HV = "hv"
ROLE_AMPLITUDE = "amplitude"

OPTICAL_ROLES = (ROLE_BLUE, ROLE_GREEN, ROLE_RED, ROLE_NIR, ROLE_SWIR1, ROLE_SWIR2)
SAR_ROLES = (ROLE_VV, ROLE_VH, ROLE_HH, ROLE_HV, ROLE_AMPLITUDE)

# Sentinel-2 band id -> role (BigEarthNet / reBEN patches ship these ids).
_S2_ID_TO_ROLE = {
    "b01": "coastal", "b02": ROLE_BLUE, "b03": ROLE_GREEN, "b04": ROLE_RED,
    "b05": ROLE_REDEDGE, "b06": ROLE_REDEDGE, "b07": ROLE_REDEDGE,
    "b08": ROLE_NIR, "b8a": ROLE_NIR, "b09": "vapour", "b10": "cirrus",
    "b11": ROLE_SWIR1, "b12": ROLE_SWIR2,
}

# Free-text synonyms that appear in band descriptions across vendors.
_NAME_TO_ROLE = {
    "blue": ROLE_BLUE, "green": ROLE_GREEN, "red": ROLE_RED,
    "nir": ROLE_NIR, "near infrared": ROLE_NIR, "near-infrared": ROLE_NIR,
    "nir08": ROLE_NIR, "nir narrow": ROLE_NIR,
    "swir": ROLE_SWIR1, "swir1": ROLE_SWIR1, "swir16": ROLE_SWIR1,
    "swir2": ROLE_SWIR2, "swir22": ROLE_SWIR2,
    "rededge": ROLE_REDEDGE, "red edge": ROLE_REDEDGE, "red-edge": ROLE_REDEDGE,
    "pan": ROLE_PAN, "panchromatic": ROLE_PAN,
    "vv": ROLE_VV, "vh": ROLE_VH, "hh": ROLE_HH, "hv": ROLE_HV,
    "sigma0_vv": ROLE_VV, "sigma0_vh": ROLE_VH,
    "amplitude": ROLE_AMPLITUDE, "intensity": ROLE_AMPLITUDE,
    "backscatter": ROLE_AMPLITUDE, "grayscale": ROLE_PAN, "gray": ROLE_PAN,
}

# Fixed layouts keyed by band count, for optical stacks.
# Values are role -> 0-based plane index.
_OPTICAL_LAYOUTS: Dict[int, Dict[str, int]] = {
    # Full Sentinel-2 L1C stack B01..B12 (B10 present).
    13: {ROLE_BLUE: 1, ROLE_GREEN: 2, ROLE_RED: 3, ROLE_NIR: 7,
         ROLE_SWIR1: 11, ROLE_SWIR2: 12, ROLE_REDEDGE: 4},
    # BigEarthNet v1/v2 Sentinel-2 patch: B01..B09,B11,B12 (no B10) = 12 planes.
    12: {ROLE_BLUE: 1, ROLE_GREEN: 2, ROLE_RED: 3, ROLE_NIR: 7,
         ROLE_SWIR1: 10, ROLE_SWIR2: 11, ROLE_REDEDGE: 4},
    # 10m+20m subset B02,B03,B04,B05,B06,B07,B08,B8A,B11,B12.
    10: {ROLE_BLUE: 0, ROLE_GREEN: 1, ROLE_RED: 2, ROLE_NIR: 6,
         ROLE_SWIR1: 8, ROLE_SWIR2: 9, ROLE_REDEDGE: 3},
    # Landsat-style 6-band reflective stack B,G,R,NIR,SWIR1,SWIR2.
    6: {ROLE_BLUE: 0, ROLE_GREEN: 1, ROLE_RED: 2, ROLE_NIR: 3,
        ROLE_SWIR1: 4, ROLE_SWIR2: 5},
}


@dataclass
class BandMap:
    """Which plane of the raw array plays which spectral role."""

    roles: Dict[str, int] = field(default_factory=dict)
    layout: str = "unknown"
    source: str = "none"           # descriptions | band_count | statistical | override | none
    notes: List[str] = field(default_factory=list)
    ambiguous: bool = False

    def has(self, *roles: str) -> bool:
        return all(r in self.roles for r in roles)

    def idx(self, role: str) -> Optional[int]:
        return self.roles.get(role)

    def available(self) -> List[str]:
        return sorted(self.roles)

    def to_audit(self) -> dict:
        return {"layout": self.layout, "source": self.source,
                "roles": dict(sorted(self.roles.items())),
                "ambiguous": self.ambiguous, "notes": list(self.notes)}


def _clean(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").strip().lower()).strip()


def _role_from_name(name: str) -> Optional[str]:
    c = _clean(name)
    if not c:
        return None
    compact = c.replace(" ", "")
    if compact in _S2_ID_TO_ROLE:
        role = _S2_ID_TO_ROLE[compact]
        return role if role in OPTICAL_ROLES + (ROLE_REDEDGE,) else None
    if c in _NAME_TO_ROLE:
        return _NAME_TO_ROLE[c]
    for key, role in _NAME_TO_ROLE.items():
        if key in c:
            return role
    m = re.fullmatch(r"b0?(\d{1,2}a?)", compact)
    if m:
        key = "b" + m.group(1).zfill(2) if not m.group(1).endswith("a") else "b8a"
        if key in _S2_ID_TO_ROLE:
            role = _S2_ID_TO_ROLE[key]
            return role if role in OPTICAL_ROLES + (ROLE_REDEDGE,) else None
    return None


def _from_descriptions(names: Sequence[Optional[str]]) -> Dict[str, int]:
    roles: Dict[str, int] = {}
    for i, name in enumerate(names):
        role = _role_from_name(name or "")
        if role and role not in roles:      # first plane wins for duplicated roles
            roles[role] = i
    return roles


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    x, y = a.ravel().astype(np.float64), b.ravel().astype(np.float64)
    if x.size > 40000:                       # subsample: correlation is stable
        step = x.size // 40000 + 1
        x, y = x[::step], y[::step]
    sx, sy = x.std(), y.std()
    if sx < 1e-12 or sy < 1e-12:
        return 0.0
    return float(np.clip(((x - x.mean()) * (y - y.mean())).mean() / (sx * sy), -1.0, 1.0))


def _resolve_bgrn(raw: Optional[np.ndarray]) -> tuple[Dict[str, int], str, bool, List[str]]:
    """Four-band visible+NIR product: NIR is last, but is plane 0 blue or red?

    Cartosat-2S MX ships B,G,R,NIR; several commercial products ship R,G,B,NIR.
    Green (plane 1) and NIR (plane 3) are identical in both, so only planes 0/2
    are in question. Vegetation reflects strongly in NIR and absorbs in red, so
    the red plane is the visible plane most anti-correlated with NIR.
    """
    default = {ROLE_BLUE: 0, ROLE_GREEN: 1, ROLE_RED: 2, ROLE_NIR: 3}
    if raw is None or raw.ndim != 3 or raw.shape[0] != 4:
        return default, "band_count", True, [
            "4-band product assumed B,G,R,NIR (Cartosat-2S MX order); no statistics available to verify."]
    nir = raw[3]
    c0, c2 = _corr(raw[0], nir), _corr(raw[2], nir)
    notes = [f"4-band order resolved statistically: corr(plane0,NIR)={c0:+.2f}, corr(plane2,NIR)={c2:+.2f}"]
    if abs(c0 - c2) < 0.05:
        notes.append("Correlation difference below 0.05: kept default B,G,R,NIR order and flagged as ambiguous.")
        return default, "band_count", True, notes
    if c0 < c2:                              # plane 0 behaves like red
        notes.append("Plane 0 is more anti-correlated with NIR -> treated as RED (R,G,B,NIR order).")
        return {ROLE_RED: 0, ROLE_GREEN: 1, ROLE_BLUE: 2, ROLE_NIR: 3}, "statistical", False, notes
    notes.append("Plane 2 is more anti-correlated with NIR -> treated as RED (B,G,R,NIR order).")
    return default, "statistical", False, notes


def resolve_bands(band_count: int, modality: str, descriptions: Sequence[Optional[str]] = (),
                  tag_names: Sequence[Optional[str]] = (), raw: Optional[np.ndarray] = None,
                  override: Optional[Dict[str, int]] = None) -> BandMap:
    """Map spectral roles onto plane indices for one image.

    ``raw`` is optional and only used for the 4-band tie-break; pass it when the
    array is already in memory.
    """
    if override:
        roles = {k: int(v) for k, v in override.items() if 0 <= int(v) < max(band_count, 1)}
        if roles:
            return BandMap(roles=roles, layout="override", source="override",
                           notes=["Band roles supplied explicitly as a permitted task parameter."])

    names = list(descriptions) or list(tag_names)
    named = _from_descriptions(names) if names else {}
    if named and any(r in named for r in (ROLE_NIR, ROLE_VV, ROLE_AMPLITUDE, ROLE_RED)):
        layout = "sar_named" if modality == "sar" else "optical_named"
        return BandMap(roles=named, layout=layout, source="descriptions",
                       notes=[f"Band roles read from product band descriptions: {names}"])

    if modality == "sar":
        return _sar_map(band_count, named)

    if modality == "panchromatic" or (band_count == 1 and modality != "sar"):
        return BandMap(roles={ROLE_PAN: 0}, layout="pan", source="band_count",
                       notes=["Single-band optical product treated as panchromatic: "
                              "spectral indices unavailable, analysis falls back to brightness + texture."])

    if band_count in _OPTICAL_LAYOUTS:
        roles = dict(_OPTICAL_LAYOUTS[band_count])
        roles = {r: i for r, i in roles.items() if i < band_count}
        return BandMap(roles=roles, layout=f"optical_{band_count}band", source="band_count",
                       notes=[f"{band_count}-band optical stack mapped with the standard "
                              f"Sentinel-2/Landsat plane order."])

    if band_count == 4:
        roles, source, ambiguous, notes = _resolve_bgrn(raw)
        return BandMap(roles=roles, layout="optical_4band", source=source,
                       notes=notes, ambiguous=ambiguous)

    if band_count == 3:
        return BandMap(roles={ROLE_RED: 0, ROLE_GREEN: 1, ROLE_BLUE: 2},
                       layout="rgb", source="band_count",
                       notes=["3-band product treated as R,G,B (benchmark PNG/JPEG convention). "
                              "No NIR/SWIR: NDVI/NDWI/NDBI unavailable, visible-only proxies used."])

    if band_count == 2:
        return BandMap(roles={ROLE_GREEN: 0, ROLE_NIR: 1}, layout="optical_2band",
                       source="band_count", ambiguous=True,
                       notes=["2-band optical product: assumed visible+NIR pair; flagged ambiguous."])

    if band_count > 13:
        roles = dict(_OPTICAL_LAYOUTS[13])
        return BandMap(roles=roles, layout="hyperspectral_prefix", source="band_count",
                       ambiguous=True,
                       notes=[f"{band_count} bands: using the Sentinel-2 plane order for the first 13 "
                              "planes; supply explicit band roles for hyperspectral products."])

    return BandMap(roles={}, layout="unknown", source="none", ambiguous=True,
                   notes=[f"Could not resolve band roles for a {band_count}-band {modality} product."])


def _sar_map(band_count: int, named: Dict[str, int]) -> BandMap:
    if named:
        return BandMap(roles=named, layout="sar_named", source="descriptions",
                       notes=["Polarimetric channels read from band descriptions."])
    if band_count == 1:
        return BandMap(roles={ROLE_AMPLITUDE: 0}, layout="sar_single", source="band_count",
                       notes=["Single-channel SAR (RISAT-style single-pol amplitude/intensity): "
                              "polarimetric ratios unavailable."])
    if band_count == 2:
        return BandMap(roles={ROLE_VV: 0, ROLE_VH: 1}, layout="sar_dual", source="band_count",
                       notes=["2-channel SAR assumed VV,VH (Sentinel-1 / BigEarthNet-S1 order)."])
    return BandMap(roles={ROLE_AMPLITUDE: 0}, layout="sar_multi", source="band_count",
                   ambiguous=True,
                   notes=[f"{band_count}-channel SAR product: first channel used as amplitude; "
                          "supply band roles for full polarimetry."])


def primary_sar_channel(bm: BandMap) -> int:
    """Plane index of the channel best suited to water/built-up discrimination."""
    for role in (ROLE_VV, ROLE_HH, ROLE_AMPLITUDE, ROLE_VH, ROLE_HV):
        if role in bm.roles:
            return bm.roles[role]
    return 0
