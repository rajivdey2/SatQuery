"""Generate demo imagery with known ground truth.

The demo inputs are synthetic, but they are not decorative: each scene is built
from an explicit label map and per-class spectral / backscatter signatures, so the
correct answer to every demo query is known in advance and written to
``ground_truth.json``. ``tests/test_analysis_accuracy.py`` asserts the analysis
engine recovers those numbers, which is what turns "the pipeline runs" into "the
pipeline measures correctly".

Scenes written to ``backend/runtime/demo_data``:

  areaA_20230101.tif    12-band Sentinel-2-like optical, t1, EPSG:32644, 10 m
  areaA_20230701.tif    same footprint at t2 with a known built-up expansion and
                        a receding water body
  areaB_optical.tif     4-band Cartosat-2S-MX-like optical with a cloud patch
  areaB_sar.tif         single-pol RISAT-like SAR of the same footprint (co-registered)
  areaB_sar_dualpol.tif Sentinel-1-like VV/VH dB product of the same footprint
  areaC_unlabelled.tif  single-band SAR with no metadata (exercises the statistical
                        modality sniffing and the low-confidence warning path)
  areaD_mismatch.tif    optical chip in a different CRS over a different footprint
                        (exercises the co-registration rejection path)
  optical_city.png      RGB benchmark-style input (PNG path, visible-only indices)
  sar_city.png          grayscale SAR-style benchmark input

Usage:  python scripts/make_demo_data.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import DEMO_DIR  # noqa: E402

SIZE = 384
PIXEL_M = 10.0
PIXEL_HA = (PIXEL_M * PIXEL_M) / 10_000.0

# Class ids inside the synthetic label map.
WATER, VEGETATION, BUILT, BARE = 0, 1, 2, 3
CLASS_NAMES = {WATER: "water", VEGETATION: "vegetation", BUILT: "built_up", BARE: "bare_soil"}

S2_BAND_NAMES = ["B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B8A", "B09", "B11", "B12"]

# Surface reflectance signatures (0..1) per class, in S2_BAND_NAMES order.
SIGNATURES: Dict[int, List[float]] = {
    WATER:      [0.050, 0.045, 0.040, 0.025, 0.020, 0.012, 0.010, 0.008, 0.007, 0.005, 0.003, 0.002],
    VEGETATION: [0.040, 0.035, 0.060, 0.035, 0.100, 0.280, 0.350, 0.400, 0.420, 0.200, 0.180, 0.080],
    BUILT:      [0.160, 0.170, 0.190, 0.210, 0.230, 0.250, 0.260, 0.270, 0.280, 0.200, 0.300, 0.280],
    BARE:       [0.140, 0.160, 0.200, 0.260, 0.300, 0.330, 0.350, 0.380, 0.385, 0.250, 0.340, 0.300],
}
CLOUD_SIGNATURE = [0.75, 0.78, 0.80, 0.80, 0.80, 0.80, 0.80, 0.80, 0.78, 0.60, 0.45, 0.35]

# Mean backscatter in dB per class, and how rough each class is.
SAR_DB = {WATER: -21.0, VEGETATION: -10.0, BUILT: -4.0, BARE: -14.5}
SAR_ROUGHNESS = {WATER: 0.4, VEGETATION: 1.1, BUILT: 3.6, BARE: 0.9}
SAR_CROSSPOL_OFFSET = {WATER: -6.0, VEGETATION: -3.0, BUILT: -7.0, BARE: -8.5}

CRS_A = "EPSG:32644"
EXTENT_A = (523000.0, 3120000.0, 523000.0 + SIZE * PIXEL_M, 3120000.0 + SIZE * PIXEL_M)
CRS_D = "EPSG:32643"
EXTENT_D = (410000.0, 2000000.0, 410000.0 + SIZE * PIXEL_M, 2000000.0 + SIZE * PIXEL_M)


# --------------------------------------------------------------------------- #
# Scene geometry
# --------------------------------------------------------------------------- #

def _disc(shape: Tuple[int, int], cy: float, cx: float, ry: float, rx: float) -> np.ndarray:
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    return (((yy - cy) / ry) ** 2 + ((xx - cx) / rx) ** 2) <= 1.0


def _rect(shape: Tuple[int, int], y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
    m = np.zeros(shape, dtype=bool)
    m[max(0, y0):min(shape[0], y1), max(0, x0):min(shape[1], x1)] = True
    return m


def base_scene(seed: int = 11) -> np.ndarray:
    """Label map: a river and a lake, farm fields, an urban block, bare ground."""
    rng = np.random.default_rng(seed)
    shape = (SIZE, SIZE)
    labels = np.full(shape, BARE, dtype=np.uint8)

    # Agricultural fields across the northern two-thirds.
    labels[: int(SIZE * 0.62), :] = VEGETATION
    for _ in range(9):                                   # fallow patches inside the fields
        cy, cx = rng.integers(10, int(SIZE * 0.6)), rng.integers(10, SIZE - 10)
        labels[_disc(shape, cy, cx, rng.integers(12, 26), rng.integers(12, 30))] = BARE

    # Urban block in the south-east, with a road grid.
    town = _rect(shape, int(SIZE * 0.63), int(SIZE * 0.95), int(SIZE * 0.55), int(SIZE * 0.95))
    labels[town] = BUILT
    for x in range(int(SIZE * 0.55), int(SIZE * 0.95), 12):
        labels[int(SIZE * 0.63):int(SIZE * 0.95), x:x + 2] = BUILT
    labels[int(SIZE * 0.40):int(SIZE * 0.42), :] = BUILT      # highway across the scene

    # East-west river with a meander, plus a lake in the north-west.
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    river_centre = int(SIZE * 0.72) + 14 * np.sin(xx / 46.0)
    labels[np.abs(yy - river_centre) <= 4.0] = WATER
    labels[_disc(shape, int(SIZE * 0.18), int(SIZE * 0.20), 26, 34)] = WATER
    return labels


def evolve_scene(labels: np.ndarray, seed: int = 23) -> Tuple[np.ndarray, Dict[str, object]]:
    """t2 = t1 plus a known, deliberate change, returned with its ground truth."""
    out = labels.copy()
    shape = labels.shape

    # 1. New construction on the north-eastern fields.
    growth = _rect(shape, int(SIZE * 0.20), int(SIZE * 0.34), int(SIZE * 0.66), int(SIZE * 0.90))
    converted_to_built = growth & (out != WATER)
    out[converted_to_built] = BUILT

    # 2. The lake recedes: its southern rim becomes bare ground.
    lake = _disc(shape, int(SIZE * 0.18), int(SIZE * 0.20), 26, 34)
    smaller_lake = _disc(shape, int(SIZE * 0.17), int(SIZE * 0.20), 19, 26)
    receded = lake & ~smaller_lake
    out[receded] = BARE

    # 3. A block of fields is harvested (vegetation -> bare).
    harvest = _rect(shape, int(SIZE * 0.05), int(SIZE * 0.15), int(SIZE * 0.10), int(SIZE * 0.34))
    harvested = harvest & (labels == VEGETATION)
    out[harvested] = BARE

    truth = {
        "built_up_gain_px": int(converted_to_built.sum()),
        "built_up_gain_ha": round(float(converted_to_built.sum()) * PIXEL_HA, 3),
        "water_loss_px": int(receded.sum()),
        "water_loss_ha": round(float(receded.sum()) * PIXEL_HA, 3),
        "vegetation_to_bare_px": int(harvested.sum()),
        "vegetation_to_bare_ha": round(float(harvested.sum()) * PIXEL_HA, 3),
        "changed_px": int((out != labels).sum()),
        "changed_fraction": round(float((out != labels).mean()), 5),
        "changed_ha": round(float((out != labels).sum()) * PIXEL_HA, 3),
    }
    return out, truth


# --------------------------------------------------------------------------- #
# Radiometry
# --------------------------------------------------------------------------- #

def optical_stack(labels: np.ndarray, seed: int = 5, cloud: bool = False,
                  bands: int = 12) -> Tuple[np.ndarray, np.ndarray]:
    """Render a label map as a multispectral reflectance stack (int16, x10000)."""
    rng = np.random.default_rng(seed)
    n = len(S2_BAND_NAMES)
    stack = np.zeros((n, *labels.shape), dtype=np.float32)
    for cls, signature in SIGNATURES.items():
        m = labels == cls
        if not m.any():
            continue
        # Per-class within-scene variability: built-up is the most heterogeneous.
        spread = {WATER: 0.10, VEGETATION: 0.18, BUILT: 0.30, BARE: 0.16}[cls]
        for b in range(n):
            noise = rng.normal(1.0, spread, size=int(m.sum())).astype(np.float32)
            stack[b][m] = signature[b] * np.clip(noise, 0.35, 2.0)

    cloud_mask = np.zeros(labels.shape, dtype=bool)
    if cloud:
        cloud_mask = _disc(labels.shape, int(SIZE * 0.30), int(SIZE * 0.78), 42, 52)
        for b in range(n):
            blend = rng.normal(1.0, 0.05, size=int(cloud_mask.sum())).astype(np.float32)
            stack[b][cloud_mask] = CLOUD_SIGNATURE[b] * np.clip(blend, 0.8, 1.2)

    stack += rng.normal(0.0, 0.004, size=stack.shape).astype(np.float32)   # sensor noise
    stack = np.clip(stack, 0.0, 1.2)
    if bands == 4:
        # Cartosat-2S MX order: blue, green, red, NIR.
        idx = [S2_BAND_NAMES.index(b) for b in ("B02", "B03", "B04", "B08")]
        stack = stack[idx]
    return (stack * 10_000.0).astype(np.int16), cloud_mask


def sar_stack(labels: np.ndarray, seed: int = 9, dual_pol: bool = False,
              looks: int = 4, as_db: bool = False, gain: float = 1200.0) -> np.ndarray:
    """Render a label map as SAR backscatter with multiplicative speckle."""
    rng = np.random.default_rng(seed)
    channels: List[np.ndarray] = []
    pols = [0, 1] if dual_pol else [0]
    for pol in pols:
        db = np.zeros(labels.shape, dtype=np.float32)
        for cls, level in SAR_DB.items():
            m = labels == cls
            if not m.any():
                continue
            offset = SAR_CROSSPOL_OFFSET[cls] if pol else 0.0
            rough = rng.normal(0.0, SAR_ROUGHNESS[cls], size=int(m.sum())).astype(np.float32)
            db[m] = level + offset + rough
        intensity = np.power(10.0, db / 10.0)
        speckle = rng.gamma(shape=looks, scale=1.0 / looks, size=labels.shape).astype(np.float32)
        intensity = intensity * speckle
        if as_db:
            channels.append((10.0 * np.log10(np.clip(intensity, 1e-9, None))).astype(np.float32))
        else:
            # Amplitude digital numbers, the RISAT-style delivery: an unknown gain
            # offsets the dB scale, which the analysis engine has to notice.
            channels.append(np.clip(np.sqrt(intensity) * gain, 0, 65535).astype(np.uint16))
    return np.stack(channels, axis=0)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

def write_geotiff(path: Path, data: np.ndarray, crs: str, extent: Tuple[float, ...],
                  band_names: List[str], tags: Dict[str, str]) -> None:
    import rasterio
    from rasterio.transform import from_bounds

    if data.ndim == 2:
        data = data[None, ...]
    count, height, width = data.shape
    transform = from_bounds(extent[0], extent[1], extent[2], extent[3], width, height)
    profile = {"driver": "GTiff", "height": height, "width": width, "count": count,
               "dtype": str(data.dtype), "crs": crs, "transform": transform,
               "compress": "deflate"}
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)
        for i, name in enumerate(band_names[:count], start=1):
            dst.set_band_description(i, name)
        if tags:
            dst.update_tags(**tags)


def class_truth(labels: np.ndarray) -> Dict[str, object]:
    total = labels.size
    return {CLASS_NAMES[c]: {"fraction": round(float((labels == c).mean()), 5),
                             "area_ha": round(float((labels == c).sum()) * PIXEL_HA, 3)}
            for c in CLASS_NAMES}


def water_bbox(labels: np.ndarray) -> Dict[str, object]:
    """Largest water region's bounding box, in 0..100 normalised coordinates."""
    from scipy import ndimage

    mask = labels == WATER
    lab, n = ndimage.label(mask)
    if n == 0:
        return {}
    counts = np.bincount(lab.ravel())
    counts[0] = 0
    comp = int(np.argmax(counts))
    ys, xs = np.nonzero(lab == comp)
    h, w = labels.shape
    return {"bbox_norm": [round(xs.min() / w * 100, 2), round(ys.min() / h * 100, 2),
                          round(xs.max() / w * 100, 2), round(ys.max() / h * 100, 2)],
            "area_ha": round(float(counts[comp]) * PIXEL_HA, 3),
            "n_water_regions": int((counts >= 64).sum())}


def main() -> None:
    demo = Path(DEMO_DIR)
    demo.mkdir(parents=True, exist_ok=True)
    from PIL import Image

    labels_t1 = base_scene()
    labels_t2, change_truth = evolve_scene(labels_t1)

    # --- bi-temporal optical pair (12-band Sentinel-2-like) -----------------
    for name, labels, date, seed in (("areaA_20230101.tif", labels_t1, "2023-01-01T04:30:00Z", 5),
                                     ("areaA_20230701.tif", labels_t2, "2023-07-01T04:31:00Z", 6)):
        stack, _ = optical_stack(labels, seed=seed)
        write_geotiff(demo / name, stack, CRS_A, EXTENT_A, S2_BAND_NAMES,
                      {"ACQUISITION_DATE": date, "SENSOR": "Sentinel-2 MSI (synthetic)",
                       "PROCESSING_LEVEL": "L2A", "DESCRIPTION": "multispectral surface reflectance"})

    # --- cross-modal pair: 4-band optical with cloud + single-pol SAR -------
    optical_b, cloud_mask = optical_stack(labels_t1, seed=17, cloud=True, bands=4)
    write_geotiff(demo / "areaB_optical.tif", optical_b, CRS_A, EXTENT_A,
                  ["blue", "green", "red", "nir"],
                  {"ACQUISITION_DATE": "2023-03-15T05:10:00Z",
                   "SENSOR": "Cartosat-2S MX (synthetic)",
                   "DESCRIPTION": "4-band multispectral, blue/green/red/NIR"})
    write_geotiff(demo / "areaB_sar.tif", sar_stack(labels_t1, seed=21), CRS_A, EXTENT_A,
                  ["amplitude"],
                  {"ACQUISITION_DATE": "2023-03-15T05:12:00Z",
                   "SENSOR": "RISAT-1 (synthetic)", "POLARISATION": "HH",
                   "DESCRIPTION": "SAR amplitude digital numbers, single polarisation"})
    write_geotiff(demo / "areaB_sar_dualpol.tif",
                  sar_stack(labels_t1, seed=22, dual_pol=True, as_db=True), CRS_A, EXTENT_A,
                  ["VV", "VH"],
                  {"ACQUISITION_DATE": "2023-03-15T05:12:00Z",
                   "SENSOR": "Sentinel-1 GRD (synthetic)",
                   "DESCRIPTION": "sigma0 VV/VH in dB, dual polarisation"})

    # --- edge cases the validator has to catch ------------------------------
    write_geotiff(demo / "areaC_unlabelled.tif", sar_stack(labels_t1, seed=33), CRS_A, EXTENT_A,
                  [""], {})
    write_geotiff(demo / "areaD_mismatch.tif", optical_stack(base_scene(seed=71), seed=41)[0],
                  CRS_D, EXTENT_D, S2_BAND_NAMES,
                  {"ACQUISITION_DATE": "2023-02-02T05:00:00Z",
                   "SENSOR": "Sentinel-2 MSI (synthetic)"})

    # --- benchmark-style PNG inputs ----------------------------------------
    stack, _ = optical_stack(labels_t1, seed=5)
    rgb = np.stack([stack[S2_BAND_NAMES.index(b)] for b in ("B04", "B03", "B02")], axis=-1)
    rgb = np.clip(rgb / 3000.0 * 255.0, 0, 255).astype(np.uint8)
    Image.fromarray(rgb).save(demo / "optical_city.png")
    sar_amp = sar_stack(labels_t1, seed=21)[0].astype(np.float32)
    lo, hi = np.percentile(sar_amp, [2, 98])
    Image.fromarray(np.clip((sar_amp - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)) \
        .save(demo / "sar_city.png")

    truth = {
        "generator": "scripts/make_demo_data.py",
        "grid": {"size": SIZE, "pixel_size_m": PIXEL_M, "pixel_area_ha": PIXEL_HA,
                 "crs": CRS_A, "extent": list(EXTENT_A)},
        "areaA_t1": {"classes": class_truth(labels_t1), "water": water_bbox(labels_t1)},
        "areaA_t2": {"classes": class_truth(labels_t2)},
        "change_areaA": change_truth,
        "areaB": {"classes": class_truth(labels_t1),
                  "cloud_fraction": round(float(cloud_mask.mean()), 5),
                  "note": "areaB_optical has a synthetic cloud patch; the SAR image does not, so "
                          "fusion should recover the obscured area from the radar evidence"},
        "areaD": {"note": "different CRS and footprint: pairing it with areaA must be rejected"},
    }
    (demo / "ground_truth.json").write_text(json.dumps(truth, indent=2), encoding="utf-8")

    print(f"demo data written to {demo}")
    for p in sorted(demo.iterdir()):
        print(f"  - {p.name:26s} {p.stat().st_size:>9,} bytes")
    print("\nground truth (areaA t1 class fractions):")
    for name, stats in truth["areaA_t1"]["classes"].items():
        print(f"  {name:12s} {stats['fraction'] * 100:5.1f}%  {stats['area_ha']:8.2f} ha")
    print(f"\nknown change: {change_truth['changed_fraction'] * 100:.1f}% of the scene, "
          f"built-up +{change_truth['built_up_gain_ha']:.1f} ha, "
          f"water -{change_truth['water_loss_ha']:.1f} ha")


if __name__ == "__main__":
    main()
