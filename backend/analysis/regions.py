"""Connected-region extraction and spatial description.

This is where a pixel mask becomes *visual evidence*: bounding boxes in the
normalised 0..100 coordinate space that VRSBench grounding uses, ground areas in
hectares where the product is georeferenced, and a spatial description ("in the
north-west quadrant") that the answer can state and a judge can check against
the overlay.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np
from scipy import ndimage

from backend.analysis.measurements import CLASS_LABELS, Region

# 3x3 grid names, row-major from the top of the image.
_COMPASS = (("north-west", "north", "north-east"),
            ("west", "centre", "east"),
            ("south-west", "south", "south-east"))
_SCREEN = (("upper-left", "top", "upper-right"),
           ("left", "centre", "right"),
           ("lower-left", "bottom", "lower-right"))


def position_word(cx: float, cy: float, north_up: bool = True) -> str:
    """Name the ninth of the frame a centroid falls in (inputs normalised 0..1)."""
    col = 0 if cx < 1 / 3 else (1 if cx < 2 / 3 else 2)
    row = 0 if cy < 1 / 3 else (1 if cy < 2 / 3 else 2)
    return (_COMPASS if north_up else _SCREEN)[row][col]


def half_word(cx: float, cy: float, north_up: bool = True) -> str:
    """Coarser description used when a region spans much of the frame."""
    dx, dy = cx - 0.5, cy - 0.5
    if abs(dx) < 0.08 and abs(dy) < 0.08:
        return "centre of the scene"
    if abs(dx) >= abs(dy):
        return ("eastern" if dx > 0 else "western") if north_up else ("right" if dx > 0 else "left")
    return ("southern" if dy > 0 else "northern") if north_up else ("lower" if dy > 0 else "upper")


def smooth_mask(mask: np.ndarray, win: int = 3) -> np.ndarray:
    """Morphological open+close: drop speckle, close pinholes, keep shape."""
    if win < 2 or mask.sum() == 0:
        return mask
    structure = np.ones((win, win), dtype=bool)
    opened = ndimage.binary_opening(mask, structure=structure)
    closed = ndimage.binary_closing(opened, structure=structure)
    return closed if closed.sum() > 0 else mask


def label_regions(mask: np.ndarray, class_name: str = "", min_pixels: int = 64,
                  pixel_area_m2: Optional[float] = None, north_up: bool = True,
                  limit: int = 8, total_valid: Optional[int] = None,
                  label: Optional[str] = None) -> List[Region]:
    """Connected components of ``mask``, largest first, as ``Region`` records."""
    m = np.asarray(mask, dtype=bool)
    if not m.any():
        return []
    labelled, n = ndimage.label(m)
    if n == 0:
        return []
    counts = np.bincount(labelled.ravel())
    counts[0] = 0
    order = np.argsort(counts)[::-1]
    h, w = m.shape
    denom = float(total_valid or m.size)
    slices = ndimage.find_objects(labelled)
    out: List[Region] = []
    for comp in order:
        if comp == 0 or counts[comp] < max(1, min_pixels):
            continue
        if len(out) >= limit:
            break
        sl = slices[comp - 1]
        if sl is None:
            continue
        ys, xs = sl
        y0, y1 = int(ys.start), int(ys.stop)
        x0, x1 = int(xs.start), int(xs.stop)
        area_px = int(counts[comp])
        comp_mask = labelled[sl] == comp
        cy_local, cx_local = ndimage.center_of_mass(comp_mask)
        cy = (y0 + float(cy_local)) / max(h, 1)
        cx = (x0 + float(cx_local)) / max(w, 1)
        bbox_area = max(1, (y1 - y0) * (x1 - x0))
        fill = area_px / bbox_area
        span = bbox_area / float(h * w)
        out.append(Region(
            label=label or CLASS_LABELS.get(class_name, class_name or "region"),
            class_name=class_name,
            bbox_norm=[round(x0 / w * 100, 2), round(y0 / h * 100, 2),
                       round(x1 / w * 100, 2), round(y1 / h * 100, 2)],
            bbox_px=[x0, y0, x1, y1],
            centroid_norm=[round(cx * 100, 2), round(cy * 100, 2)],
            area_px=area_px,
            area_ha=round(area_px * pixel_area_m2 / 10_000.0, 3) if pixel_area_m2 else None,
            fraction=round(area_px / denom, 5) if denom else 0.0,
            position=(half_word(cx, cy, north_up) if span > 0.45 else position_word(cx, cy, north_up)),
            compactness=round(float(fill), 3),
            score=round(float(min(1.0, 0.45 + 0.4 * fill + 0.15 * min(1.0, area_px / max(denom, 1) * 8))), 3)))
    return out


def region_count(mask: np.ndarray, min_pixels: int = 64) -> int:
    """Number of components above the size floor (used to answer "how many...")."""
    m = np.asarray(mask, dtype=bool)
    if not m.any():
        return 0
    labelled, n = ndimage.label(m)
    if n == 0:
        return 0
    counts = np.bincount(labelled.ravel())
    counts[0] = 0
    return int((counts >= max(1, min_pixels)).sum())


def mask_from_regions(shape: Tuple[int, int], regions: List[Region]) -> np.ndarray:
    """Rasterise region bounding boxes back to a mask (overlay rendering)."""
    out = np.zeros(shape, dtype=bool)
    h, w = shape
    for r in regions:
        if len(r.bbox_px) == 4:
            x0, y0, x1, y1 = r.bbox_px
        elif len(r.bbox_norm) == 4:
            x0 = int(r.bbox_norm[0] / 100 * w)
            y0 = int(r.bbox_norm[1] / 100 * h)
            x1 = int(r.bbox_norm[2] / 100 * w)
            y1 = int(r.bbox_norm[3] / 100 * h)
        else:
            continue
        out[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = True
    return out


def dominant_direction(mask: np.ndarray, north_up: bool = True) -> str:
    """Where the bulk of a scattered mask sits (used for change locations)."""
    m = np.asarray(mask, dtype=bool)
    if not m.any():
        return "no location"
    h, w = m.shape
    ys, xs = np.nonzero(m)
    cy, cx = ys.mean() / max(h, 1), xs.mean() / max(w, 1)
    spread = float(np.sqrt(((ys / h - cy) ** 2 + (xs / w - cx) ** 2).mean()))
    if spread > 0.28:
        return "distributed across the scene"
    return position_word(cx, cy, north_up)
