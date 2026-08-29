"""Generate synthetic demo data for smoke tests and the judge demo.

Writes into backend/runtime/demo_data/:
  optical_city.png         single-image VQA/caption/grounding (benchmark-style)
  sar_city.png             single-image SAR-style input
  areaA_20230101.tif       georeferenced optical, t0 (filename carries the date)
  areaA_20230701.tif       georeferenced optical, t1 (same CRS/extent -> bi-temporal)
  areaB_optical.tif        georeferenced optical  (EPSG:32644)
  areaB_sar.tif            georeferenced SAR      (same CRS/extent -> cross-modal pair)
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from backend.config import UPLOAD_DIR

DEMO_DIR = UPLOAD_DIR.parent / "demo_data"


def _blob(h: int, w: int, seed: int, n: int = 8) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w), dtype=np.float32)
    for _ in range(n):
        y, x = rng.integers(0, h), rng.integers(0, w)
        ry, rx = rng.integers(20, 90), rng.integers(20, 110)
        yy, xx = np.mgrid[0:h, 0:w]
        img += 255 * np.exp(-(((yy - y) / ry) ** 2 + ((xx - x) / rx) ** 2))
    return np.clip(img, 0, 255).astype(np.uint8)


def _optical_city(seed: int = 7) -> np.ndarray:
    h = w = 512
    rng = np.random.default_rng(seed)
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    rgb[..., 0] = _blob(h, w, seed, 7)          # urban-ish reds
    rgb[..., 1] = _blob(h, w, seed + 1, 5)      # green vegetation
    rgb[..., 2] = _blob(h, w, seed + 2, 4)      # water reflectance
    # add a river band across the scene bottom
    river = np.zeros((h, w), dtype=np.uint8)
    for x in range(w):
        river[int(h * 0.72 + 18 * np.sin(x / 64.0)), x] = 255
    from scipy import ndimage

    river = ndimage.gaussian_filter(river, sigma=6)
    blue = np.clip(rgb[..., 2].astype(float) + river * 0.6, 0, 255).astype(np.uint8)
    rgb[..., 2] = blue
    return rgb


def _sar_like(seed: int = 3) -> np.ndarray:
    h = w = 512
    rng = np.random.default_rng(seed)
    base = _blob(h, w, seed, 9).astype(np.float32)
    speckle = np.random.gamma(shape=3.0, scale=base / 3.0 + 1.5, size=(h, w)).astype(np.float32)
    # bright built-up streaks
    for _ in range(6):
        y = rng.integers(0, h)
        speckle[y:y + 3, :] += 120
    return np.clip(speckle, 0, 255).astype(np.uint8)


def _write_geotiff(path: Path, rgb: np.ndarray | None, sar: np.ndarray | None,
                   crs: str, extent) -> None:
    import rasterio
    from rasterio.transform import from_bounds

    if rgb is not None:
        height, width, _ = rgb.shape
        data = np.transpose(rgb, (2, 0, 1))
        count = 3
        dtype = "uint8"
    else:
        height, width = sar.shape
        data = sar[None, ...]
        count = 1
        dtype = "uint8"
    transform = from_bounds(extent[0], extent[1], extent[2], extent[3], width, height)
    profile = {"driver": "GTiff", "height": height, "width": width, "count": count,
               "dtype": dtype, "crs": crs, "transform": transform}
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)
        dst.update_tags(ACQUISITION_DATE=os.getenv("DEMO_ACQ_DATE", ""))


def main() -> None:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)

    from PIL import Image

    Image.fromarray(_optical_city()).save(DEMO_DIR / "optical_city.png")
    Image.fromarray(_sar_like()).save(DEMO_DIR / "sar_city.png")

    # Georeferenced bi-temporal optical pair (EPSG:4326)
    ext_a = [72.80, 19.00, 72.84, 19.04]
    os.environ["DEMO_ACQ_DATE"] = "2023-01-01T04:30:00Z"
    _write_geotiff(DEMO_DIR / "areaA_20230101.tif", _optical_city(11), None, "EPSG:4326", ext_a)
    os.environ["DEMO_ACQ_DATE"] = "2023-07-01T04:31:00Z"
    _write_geotiff(DEMO_DIR / "areaA_20230701.tif", _optical_city(12), None, "EPSG:4326", ext_a)

    # Georeferenced cross-modal pair (UTM zone 44)
    ext_b = [523000, 3120000, 524020, 3121000]
    _write_geotiff(DEMO_DIR / "areaB_optical.tif", _optical_city(21), None, "EPSG:32644", ext_b)
    _write_geotiff(DEMO_DIR / "areaB_sar.tif", None, _sar_like(22), "EPSG:32644", ext_b)

    print(f"demo data written to {DEMO_DIR}:")


if __name__ == "__main__":
    main()
    for p in sorted(DEMO_DIR.iterdir()):
        print("  -", p.name, p.stat().st_size, "bytes")