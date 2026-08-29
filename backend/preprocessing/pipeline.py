"""Turn uploaded files into specialist-ready display arrays + preview PNGs.

Modality-aware rendering: SAR products go through the dB/speckle/normalize
pipeline, everything else through optical percentile normalization.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

import numpy as np

from backend.config import OUTPUT_DIR
from backend.preprocessing import geotiff_io as gio
from backend.preprocessing import sar_ops as sar
from backend.preprocessing.geotiff_io import ImageInfo


@dataclass
class PreparedImage:
    file: str
    rgb: np.ndarray  # (H, W, 3) uint8
    info: ImageInfo
    preview_path: str = ""


def render_image(path: str) -> tuple:
    """Return (rgb HxWx3 uint8, info) for one file."""
    info = gio.probe_image(path)
    raw, _ = gio.read_array(path)
    if info.modality == "sar":
        rgb = sar.sar_to_display(raw)
    else:
        rgb = gio.to_display_rgb(raw)
    return rgb, info


def make_preview(rgb: np.ndarray, stem: str, out_dir: Path | None = None) -> Path:
    """Persist a PNG preview served back to the UI."""
    import PIL.Image

    out_dir = out_dir or OUTPUT_DIR
    dest = out_dir / f"{stem}.png"
    PIL.Image.fromarray(rgb).save(dest)
    return dest


def prepare_image(path: str) -> PreparedImage:
    rgb, info = render_image(path)
    return PreparedImage(file=path, rgb=rgb, info=info)


def prepare_report_images(images: List[PreparedImage], run_id: str) -> List[PreparedImage]:
    """Render previews for every prepared image and attach paths."""
    for im in images:
        if not im.preview_path:
            stem = f"{run_id}_{Path(im.file).stem}"
            im.preview_path = str(make_preview(im.rgb, stem))
    return images