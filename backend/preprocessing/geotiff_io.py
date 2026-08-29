"""GeoTIFF/TIFF reading, probing, modality sniffing, display rendering and tiling.

rasterio/GDAL is imported lazily so the API serves mocks even before the
geospatial stack is installed.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

from backend.config import OUTPUT_DIR, settings

SUPPORTED_EXTS = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}
SAR_HINTS = {
    "sentinel-1", "sar", "risat", "scattering", "backscatter", "vv", "vh",
    "amplitude", "sigma0", "gamma0", "beta0", "polsar", "spaceborne imaging",
    "capella", "iceye", "radarsat", "terrasar",
}
MULTISPECTRAL_HINTS = {
    "sentinel-2", "multispectral", "landsat", "band", "b1", "b2", "b3", "b4",
    "coastal", "nearest_reflectance", "cartosat",
}
PAN_HINTS = {"panchromatic", "pan", "cartosat-2s", "cartosat-2"}


@dataclass
class ImageInfo:
    path: str
    format: str
    width: int
    height: int
    band_count: int
    dtype: str
    modality: str = "unknown"
    crs: Optional[str] = None
    extent: Optional[List[float]] = None
    pixel_size: Optional[List[float]] = None
    acquisition_date: Optional[str] = None
    metadata_source: str = "none"
    tags: dict = field(default_factory=dict)


def _norm_ext(path: str) -> str:
    return Path(path).name.lower()


def detect_modality(band_count: int, dtype: str, tags: dict, filename: str) -> Tuple[str, str]:
    """Return (modality, metadata_source). Prefers metadata tags over pixel stats (per PS)."""
    desc = " ".join(
        str(v).lower() for k, v in tags.items()
        if k.lower() in {"description", "imagedescription", "bandnames", "samplesovertag",
                         "instrumentid", "productname", "tiff<0x010e>", "software", "copyright"}
    )
    name = filename.lower()

    if any(h in desc or h in name for h in SAR_HINTS):
        # dual-pol VV/VH appears as 2 bands; single-pol RISAT as 1 band.
        return "sar", "tags"
    if band_count >= 4:
        if any(h in desc or h in name for h in MULTISPECTRAL_HINTS):
            return "multispectral", "tags"
        return "multispectral", "band_count"
    if band_count == 3 and dtype in {"uint8", "int16", "uint16"}:
        return "optical", "band_count"
    if band_count == 1:
        if any(h in desc or h in name for h in PAN_HINTS):
            return "panchromatic", "tags"
        # Single band, no metadata hint: plausible SAR (amplitude) or pan. Flag as unknown.
        return "unknown", "tags" if tags else "none"
    if band_count == 2:
        return "sar", "band_count"
    return "unknown", "none"


def _parse_date_from_tags(tags: dict) -> Optional[str]:
    for key in ("datetime", "acquisitiondatetime", "acquisitiondate", "acquisition_time", "starttime"):
        v = tags.get(key)
        if v:
            try:
                return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date().isoformat()
            except Exception:
                try:
                    return date.fromisoformat(str(v)[:10]).isoformat()
                except Exception:
                    continue
    return None


def _parse_date_from_name(filename: str) -> Optional[str]:
    """Common satellite product filenames embed dates like YYYYMMDD."""
    m = re.search(r"(20\d{2})[_-]?(\d{2})[_-]?(\d{2})", filename)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            try:
                return date(y, mo, d).isoformat()
            except ValueError:
                return None
    m = re.search(r"(20\d{6})", filename)
    if m:
        s = m.group(1)
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8])).isoformat()
        except ValueError:
            return None
    return None


def probe_image(path: str) -> ImageInfo:
    ext = Path(path).suffix.lower()
    tags: dict = {}
    crs = extent = pixel = dtype = None
    width = height = bands = 0
    if ext in {".tif", ".tiff"}:
        import rasterio

        with rasterio.open(path) as src:
            width, height = src.width, src.height
            bands = src.count
            dtype = src.dtypes[0] if src.dtypes else ""
            crs = str(src.crs) if src.crs else None
            if src.transform:
                res = src.res
                pixel = [float(res[0]), float(res[1])]
                try:
                    b = src.bounds
                    extent = [float(b.left), float(b.bottom), float(b.right), float(b.top)]
                except Exception:
                    extent = None
            tags = dict(src.tags().items())
            for k in ("DATETIME", "ACQUISITION_DATE", "AcquisitionTime", "acquisition_date"):
                if k in src.tags(ns="IMD"):
                    tags.setdefault("acquisitiondate", str(src.tags(ns="IMD")[k]))
    else:  # PNG / JPEG (benchmark inputs only, per PS)
        with Image.open(path) as im:
            width, height = im.width, im.height
            bands = len(im.getbands())
            dtype = {"L": "uint8", "RGB": "uint8", "RGBA": "uint8"}.get(im.mode, "uint8")
        tags = {}

    modality, src_name = detect_modality(bands, dtype or "", tags, Path(path).name)
    acq = _parse_date_from_tags(tags) or _parse_date_from_name(Path(path).name)
    return ImageInfo(
        path=path,
        format="GEOTIFF" if ext in {".tif", ".tiff"} and crs else ("TIFF" if ext in {".tif", ".tiff"} else "PNG" if ext == ".png" else "JPEG"),
        width=width,
        height=height,
        band_count=bands,
        dtype=dtype or "",
        modality=modality,
        crs=crs,
        extent=extent,
        pixel_size=pixel,
        acquisition_date=acq,
        metadata_source=src_name if (tags or acq) else "none",
        tags=tags,
    )


def read_array(path: str, bands: Optional[List[int]] = None) -> Tuple[np.ndarray, dict]:
    """Read raster as (H,W) or (C,H,W) numpy array + profile copy."""
    ext = Path(path).suffix.lower()
    if ext in {".tif", ".tiff"}:
        import rasterio

        with rasterio.open(path) as src:
            idx = bands or list(range(1, src.count + 1))
            arr = src.read(idx)
            profile = dict(src.profile)
        return arr.squeeze(), profile
    arr = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
    return np.transpose(arr, (2, 0, 1)), {}


def percentile_normalize(a: np.ndarray, pmin: float = 2.0, pmax: float = 98.0) -> np.ndarray:
    lo, hi = np.percentile(a, [pmin, pmax])
    if hi <= lo:
        hi = lo + 1e-6
    return np.clip((a.astype(np.float64) - lo) / (hi - lo), 0.0, 1.0)


def to_display_rgb(arr: np.ndarray) -> np.ndarray:
    """Best-effort (H,W,3) uint8 from (H,W) or (C,H,W) input."""
    a = arr
    if a.ndim == 3:
        # Spectral grouping: pick a representative band triplet.
        c = a.shape[0]
        if c == 1:
            vis = a[0]
        elif c == 2:
            vis = (a[0] + a[1]) / 2
        else:
            idx = (0, int(c // 2), c - 1)
            vis = np.stack([a[i] for i in idx], axis=-1)
    else:
        vis = a
    if vis.ndim == 2:
        vis = np.stack([vis] * 3, axis=-1)
    else:
        vis = vis[..., :3]
    if vis.dtype != np.uint8:
        norm = percentile_normalize(vis.astype(np.float64))
        vis = (norm * 255).astype(np.uint8)
    return np.ascontiguousarray(vis)


def render_preview(path: str, out_dir: Optional[Path] = None, max_side: int = 1024) -> Path:
    """Write a display-safe PNG thumbnail next to 'uploads', return its path."""
    out_dir = out_dir or OUTPUT_DIR
    arr, _ = read_array(path)
    rgb = to_display_rgb(arr)
    h, w = rgb.shape[:2]
    scale = min(1.0, max_side / max(h, w))
    if scale < 1.0:
        rgb = np.asarray(Image.fromarray(rgb).resize((int(w * scale), int(h * scale)), Image.LANCZOS))
    name = Path(path).stem + ".png"
    dest = out_dir / name
    Image.fromarray(rgb).save(dest)
    return dest


def cache_key(*paths: str) -> str:
    key = "|".join(str(p) for p in paths)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


@dataclass
class Tile:
    index: int
    array: np.ndarray  # (H,W,3) uint8
    x0: int
    y0: int
    x1: int
    y1: int


def tile_rgb(rgb: np.ndarray, tile_size: Optional[int] = None, max_tiles: Optional[int] = None, pad: int = 0) -> List[Tile]:
    """Slice a (H,W,3) image into fixed-size tiles with fractional scale-down."""
    tile_size = tile_size or settings.tile_size
    max_tiles = max_tiles or settings.max_tiles
    h, w = rgb.shape[:2]
    # Downscale huge rasters first so tiles carry context.
    scale = min(1.0, (tile_size * max_tiles * 0.9) / max(h, w))
    if scale < 1.0 and h * w > tile_size * tile_size * max_tiles:
        rgb = np.asarray(Image.fromarray(rgb).resize((int(w * scale), int(h * scale)), Image.LANCZOS))
        h, w = rgb.shape[:2]
    tiles: List[Tile] = []
    for j in range(0, h, tile_size):
        for i in range(0, w, tile_size):
            if len(tiles) >= max_tiles:
                return tiles
            x0, y0 = max(0, i - pad), max(0, j - pad)
            x1, y1 = min(w, i + tile_size + pad), min(h, j + tile_size + pad)
            tiles.append(Tile(len(tiles), rgb[y0:y1, x0:x1], x0, y0, x1, y1))
    return tiles