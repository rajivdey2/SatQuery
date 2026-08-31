"""Turn uploaded files into analysis-ready arrays.

Earlier revisions of this module kept only an 8-bit display composite, which
silently made every spectral measurement impossible -- an NDVI cannot be computed
from a stretched RGB thumbnail. ``PreparedImage`` therefore carries the *raw*
band stack, the resolved band roles, a validity mask, calibrated SAR channels in
dB, and the ground area of one pixel, alongside the display preview.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from backend.config import OUTPUT_DIR, settings
from backend.preprocessing import bands as bandmod
from backend.preprocessing import geotiff_io as gio
from backend.preprocessing import sar_ops as sar
from backend.preprocessing.bands import BandMap
from backend.preprocessing.geotiff_io import ImageInfo
from backend.preprocessing.sar_ops import SarChannel

_DEG_LAT_M = 110_574.0
_DEG_LON_M = 111_320.0


@dataclass
class PreparedImage:
    """One input image, ready for both the analysis engine and a VLM."""

    file: str
    info: ImageInfo
    raw: np.ndarray                       # (C, H, W) float32, NaN where nodata
    bands: BandMap
    rgb: np.ndarray                       # (H, W, 3) uint8 display composite
    valid: np.ndarray                     # (H, W) bool
    sar_channels: Dict[str, SarChannel] = field(default_factory=dict)
    preview_path: str = ""
    read_scale: float = 1.0               # linear decimation applied on read
    pixel_area_m2: Optional[float] = None
    notes: List[str] = field(default_factory=list)

    @property
    def shape(self) -> tuple[int, int]:
        return self.raw.shape[1], self.raw.shape[2]

    @property
    def modality(self) -> str:
        return self.info.modality

    @property
    def is_sar(self) -> bool:
        return self.info.modality == "sar"

    def band(self, role: str) -> Optional[np.ndarray]:
        i = self.bands.idx(role)
        return self.raw[i] if i is not None and i < self.raw.shape[0] else None

    def primary_sar_db(self) -> Optional[np.ndarray]:
        if not self.sar_channels:
            return None
        for role in ("vv", "hh", "amplitude", "vh", "hv"):
            if role in self.sar_channels:
                return self.sar_channels[role].db
        return next(iter(self.sar_channels.values())).db

    def area_ha(self, n_pixels: float) -> Optional[float]:
        if not self.pixel_area_m2:
            return None
        return float(n_pixels) * self.pixel_area_m2 / 10_000.0

    def to_audit(self) -> dict:
        out = self.info.to_audit()
        out["band_assignment"] = self.bands.to_audit()
        out["analysis_shape"] = [self.shape[0], self.shape[1]]
        out["read_decimation"] = round(self.read_scale, 3)
        out["pixel_area_m2"] = round(self.pixel_area_m2, 3) if self.pixel_area_m2 else None
        out["valid_pixel_fraction"] = round(float(self.valid.mean()), 4)
        if self.sar_channels:
            out["sar_channels"] = {k: v.to_audit() for k, v in self.sar_channels.items()}
        if self.notes:
            out["preprocessing_notes"] = list(self.notes)
        return out


def _pixel_area_m2(info: ImageInfo, read_scale: float) -> Optional[float]:
    """Ground area of one *analysis* pixel in m^2, geographic CRS included."""
    if not info.pixel_size:
        return None
    dx, dy = float(info.pixel_size[0]) * read_scale, float(info.pixel_size[1]) * read_scale
    if dx <= 0 or dy <= 0:
        return None
    crs = (info.crs or "").lower()
    geographic = ("4326" in crs or "wgs 84" in crs or "longlat" in crs or "degree" in crs)
    if geographic or (dx < 0.01 and dy < 0.01):
        lat = 0.0
        if info.extent_wgs84:
            lat = (info.extent_wgs84[1] + info.extent_wgs84[3]) / 2.0
        elif info.extent:
            lat = (info.extent[1] + info.extent[3]) / 2.0
        mx = dx * _DEG_LON_M * max(math.cos(math.radians(lat)), 1e-3)
        my = dy * _DEG_LAT_M
        return float(mx * my)
    return float(dx * dy)


def _valid_mask(raw: np.ndarray, nodata: Optional[float]) -> np.ndarray:
    valid = np.all(np.isfinite(raw), axis=0)
    if nodata is not None:
        valid &= ~np.all(np.isclose(np.nan_to_num(raw, nan=nodata + 1.0), nodata), axis=0)
    # A raster of all-zero pixels at the border is the usual footprint padding.
    zeros = np.all(np.nan_to_num(raw) == 0, axis=0)
    if 0.0 < zeros.mean() < 0.9:
        valid &= ~zeros
    return valid


def prepare_image(path: str, speckle_filter: Optional[bool] = None,
                 band_override: Optional[Dict[str, int]] = None,
                 info: Optional[ImageInfo] = None) -> PreparedImage:
    """Read one file into an analysis-ready ``PreparedImage``."""
    info = info or gio.probe_image(path)
    raw, profile = gio.read_array(path)
    if raw.ndim == 2:
        raw = raw[None, ...]

    read_h, read_w = raw.shape[1], raw.shape[2]
    read_scale = (info.width / read_w) if read_w else 1.0
    notes: List[str] = []
    if read_scale > 1.01:
        notes.append(f"Raster decimated {read_scale:.2f}x on read "
                     f"({info.width}x{info.height} -> {read_w}x{read_h}) to stay inside "
                     f"the {settings.max_analysis_pixels:,}-pixel analysis budget; "
                     f"areas are reported in ground units so the decimation does not bias them.")

    band_map = bandmod.resolve_bands(
        band_count=raw.shape[0], modality=info.modality,
        descriptions=info.descriptions, raw=raw, override=band_override)
    notes.extend(band_map.notes)

    valid = _valid_mask(raw, info.nodata)
    if valid.mean() < 1.0:
        notes.append(f"{(1.0 - float(valid.mean())) * 100:.1f}% of pixels excluded as nodata/footprint padding.")

    sar_channels: Dict[str, SarChannel] = {}
    if info.modality == "sar":
        use_filter = settings.speckle_filter if speckle_filter is None else bool(speckle_filter)
        for role in bandmod.SAR_ROLES:
            i = band_map.idx(role)
            if i is None or i >= raw.shape[0]:
                continue
            ch = sar.prepare_channel(raw[i], speckle_filter=use_filter)
            sar_channels[role] = ch
            notes.extend(f"{role.upper()}: {n}" for n in ch.notes)
        if not sar_channels:
            ch = sar.prepare_channel(raw[0], speckle_filter=use_filter)
            sar_channels["amplitude"] = ch
            notes.extend(f"AMPLITUDE: {n}" for n in ch.notes)
        rgb = sar.sar_to_display(raw if raw.shape[0] > 1 else raw[0], speckle_filter=use_filter)
    else:
        rgb = gio.to_display_rgb(raw, band_map)

    return PreparedImage(
        file=path, info=info, raw=raw, bands=band_map, rgb=rgb, valid=valid,
        sar_channels=sar_channels, read_scale=read_scale,
        pixel_area_m2=_pixel_area_m2(info, read_scale), notes=notes)


def make_preview(rgb: np.ndarray, stem: str, out_dir: Optional[Path] = None,
                 max_side: Optional[int] = None) -> Path:
    """Persist a PNG preview served back to the UI and embedded in the report."""
    from PIL import Image as PILImage

    out_dir = Path(out_dir or OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    img = PILImage.fromarray(np.ascontiguousarray(rgb))
    cap = max_side or settings.preview_max_side
    if max(img.size) > cap:
        scale = cap / max(img.size)
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                         PILImage.LANCZOS)
    dest = out_dir / f"{stem}.png"
    img.save(dest)
    return dest


def prepare_report_images(images: List[PreparedImage], run_id: str) -> List[PreparedImage]:
    for im in images:
        if not im.preview_path:
            im.preview_path = str(make_preview(im.rgb, f"{run_id}_{Path(im.file).stem}"))
    return images


def align_pair(a: PreparedImage, b: PreparedImage) -> PreparedImage:
    """Resample ``b`` onto ``a``'s pixel grid so per-pixel comparison is valid.

    The validator has already confirmed CRS/extent agreement; what remains is a
    possible resolution difference (a 0.65 m Cartosat chip against a 3 m RISAT
    chip of the same footprint). Returns a shallow copy of ``b`` on ``a``'s grid.
    """
    if b.shape == a.shape:
        return b
    th, tw = a.shape
    raw = gio.resample_to(b.raw, (th, tw))
    valid = gio.resample_to(b.valid.astype(np.float32), (th, tw)) > 0.5
    sar_channels = {k: SarChannel(db=gio.resample_to(v.db, (th, tw)), convention=v.convention,
                                  enl=v.enl, speckle_filtered=v.speckle_filtered, notes=v.notes)
                    for k, v in b.sar_channels.items()}
    rgb = np.asarray(np.clip(gio.resample_to(
        np.transpose(b.rgb.astype(np.float32), (2, 0, 1)), (th, tw)), 0, 255), dtype=np.uint8)
    rgb = np.transpose(rgb, (1, 2, 0))
    scaled_area = None
    if b.pixel_area_m2:
        scaled_area = b.pixel_area_m2 * (b.shape[0] * b.shape[1]) / float(th * tw)
    return PreparedImage(
        file=b.file, info=b.info, raw=raw, bands=b.bands, rgb=rgb, valid=valid,
        sar_channels=sar_channels, preview_path=b.preview_path, read_scale=b.read_scale,
        pixel_area_m2=scaled_area,
        notes=list(b.notes) + [f"Resampled from {b.shape[0]}x{b.shape[1]} to {th}x{tw} "
                               "onto the reference image grid for per-pixel comparison."])


def render_image(path: str) -> tuple:
    """Backwards-compatible helper: ``(display_rgb, info)``."""
    prepared = prepare_image(path)
    return prepared.rgb, prepared.info


def prepared_from_array(raw: np.ndarray, modality: str, roles: Optional[Dict[str, int]] = None,
                        name: str = "in-memory", pixel_size: Optional[List[float]] = None,
                        crs: Optional[str] = None, acquisition_date: Optional[str] = None,
                        speckle_filter: Optional[bool] = None) -> PreparedImage:
    """Build a ``PreparedImage`` from an in-memory band stack.

    Used by the BigEarthNet-MM trainer (patches arrive as one GeoTIFF per band, so
    there is no single file to open) and by the tests, which need deterministic
    synthetic scenes. Going through the same object as the serving path is what
    guarantees train-time and inference-time features are identical.
    """
    arr = np.asarray(raw, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr[None, ...]
    info = ImageInfo(
        path=name, format="GEOTIFF" if crs else "TIFF", width=arr.shape[2], height=arr.shape[1],
        band_count=arr.shape[0], dtype=str(arr.dtype), modality=modality,
        modality_confidence="high", modality_reason="declared by the caller",
        crs=crs, pixel_size=pixel_size, acquisition_date=acquisition_date,
        metadata_source="none" if not acquisition_date else "rasterio_tags")
    if crs and pixel_size:
        info.extent = [0.0, 0.0, pixel_size[0] * arr.shape[2], pixel_size[1] * arr.shape[1]]

    band_map = bandmod.resolve_bands(band_count=arr.shape[0], modality=modality,
                                     raw=arr, override=roles)
    valid = _valid_mask(arr, None)
    sar_channels: Dict[str, SarChannel] = {}
    if modality == "sar":
        use_filter = settings.speckle_filter if speckle_filter is None else bool(speckle_filter)
        for role in bandmod.SAR_ROLES:
            i = band_map.idx(role)
            if i is not None and i < arr.shape[0]:
                sar_channels[role] = sar.prepare_channel(arr[i], speckle_filter=use_filter)
        if not sar_channels:
            sar_channels["amplitude"] = sar.prepare_channel(arr[0], speckle_filter=use_filter)
        rgb = sar.sar_to_display(arr if arr.shape[0] > 1 else arr[0], speckle_filter=use_filter)
    else:
        rgb = gio.to_display_rgb(arr, band_map)
    return PreparedImage(file=name, info=info, raw=arr, bands=band_map, rgb=rgb, valid=valid,
                         sar_channels=sar_channels, read_scale=1.0,
                         pixel_area_m2=_pixel_area_m2(info, 1.0), notes=list(band_map.notes))
