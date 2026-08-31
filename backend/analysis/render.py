"""Visual-evidence rendering.

The problem statement asks for *visual evidence*, not descriptions of evidence,
so every specialist that produces a mask or a box also produces a PNG the GUI
and the PDF report can show: bounding-box overlays for grounding, a colour-coded
land-cover map, a red change map for the bi-temporal task, and a provenance map
for optical+SAR fusion that shows which sensor contributed each pixel.

PIL only, deliberately -- adding matplotlib to the serving path for four overlays
is not worth the dependency.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw

from backend.analysis.measurements import (BARE_SOIL, BUILT_UP, CLASS_LABELS, OTHER,
                                           VEGETATION, WATER, Region)
from backend.config import OUTPUT_DIR

CLASS_COLORS: Dict[str, Tuple[int, int, int]] = {
    WATER: (33, 113, 181),
    VEGETATION: (35, 139, 69),
    BUILT_UP: (222, 45, 38),
    BARE_SOIL: (196, 156, 90),
    OTHER: (140, 140, 140),
}
BOX_COLOR = (255, 61, 61)
CHANGE_COLOR = (255, 40, 40)
_LEGEND_ROW = 16
_LEGEND_PAD = 6


def _as_image(rgb: np.ndarray) -> Image.Image:
    a = np.asarray(rgb)
    if a.ndim == 2:
        a = np.stack([a] * 3, axis=-1)
    if a.dtype != np.uint8:
        a = np.clip(a, 0, 255).astype(np.uint8)
    return Image.fromarray(np.ascontiguousarray(a[..., :3])).convert("RGB")


def _blend(base: Image.Image, mask: np.ndarray, color: Tuple[int, int, int],
           alpha: float = 0.45) -> Image.Image:
    """Alpha-blend a solid colour into ``base`` wherever ``mask`` is true."""
    m = np.asarray(mask, dtype=bool)
    if m.shape != (base.height, base.width):
        m = np.asarray(Image.fromarray(m.astype(np.uint8) * 255).resize(
            (base.width, base.height), Image.NEAREST)) > 127
    arr = np.asarray(base, dtype=np.float32).copy()
    tint = np.asarray(color, dtype=np.float32)
    arr[m] = (1.0 - alpha) * arr[m] + alpha * tint
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def _outline(mask: np.ndarray) -> np.ndarray:
    from scipy import ndimage

    m = np.asarray(mask, dtype=bool)
    if not m.any():
        return m
    return m & ~ndimage.binary_erosion(m, structure=np.ones((3, 3), dtype=bool))


def _legend(img: Image.Image, entries: Sequence[Tuple[str, Tuple[int, int, int]]]) -> Image.Image:
    """Append a legend strip below the image so the colours are self-explaining."""
    if not entries:
        return img
    rows = len(entries)
    strip_h = rows * _LEGEND_ROW + 2 * _LEGEND_PAD
    out = Image.new("RGB", (img.width, img.height + strip_h), (18, 20, 24))
    out.paste(img, (0, 0))
    dr = ImageDraw.Draw(out)
    y = img.height + _LEGEND_PAD
    for text, color in entries:
        dr.rectangle([_LEGEND_PAD, y + 3, _LEGEND_PAD + 12, y + 13], fill=color,
                     outline=(240, 240, 240))
        dr.text((_LEGEND_PAD + 18, y + 2), text[:110], fill=(235, 235, 235))
        y += _LEGEND_ROW
    return out


def _save(img: Image.Image, stem: str, out_dir: Optional[Path] = None) -> str:
    out_dir = Path(out_dir or OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{stem}.png"
    img.save(dest)
    return str(dest)


def draw_boxes(rgb: np.ndarray, regions: Sequence[Region], stem: str,
               out_dir: Optional[Path] = None, mask: Optional[np.ndarray] = None,
               color: Tuple[int, int, int] = BOX_COLOR) -> str:
    """Bounding boxes (plus optional class outline) over the display image."""
    img = _as_image(rgb)
    if mask is not None:
        img = _blend(img, _outline(mask), color, alpha=0.85)
    dr = ImageDraw.Draw(img)
    width = max(2, img.width // 300)
    for i, r in enumerate(regions):
        if len(r.bbox_norm) != 4:
            continue
        x1 = r.bbox_norm[0] / 100.0 * img.width
        y1 = r.bbox_norm[1] / 100.0 * img.height
        x2 = r.bbox_norm[2] / 100.0 * img.width
        y2 = r.bbox_norm[3] / 100.0 * img.height
        dr.rectangle([x1, y1, x2, y2], outline=color, width=width)
        caption = f"{r.label}" + (f" · {r.area_ha:.1f} ha" if r.area_ha else "")
        ty = max(0.0, y1 - 13)
        dr.rectangle([x1, ty, x1 + 7.2 * len(caption[:44]), ty + 12], fill=(0, 0, 0))
        dr.text((x1 + 2, ty + 1), caption[:44], fill=(255, 255, 255))
    entries = [(f"{r.label} ({r.position}, score {r.score:.2f})", color) for r in regions[:4]]
    return _save(_legend(img, entries), stem, out_dir)


def render_class_map(rgb: np.ndarray, masks: Dict[str, np.ndarray], stem: str,
                     out_dir: Optional[Path] = None, alpha: float = 0.45,
                     stats: Optional[Dict[str, float]] = None) -> str:
    """Colour-coded land-cover overlay with a legend."""
    img = _as_image(rgb)
    entries: List[Tuple[str, Tuple[int, int, int]]] = []
    for name, color in CLASS_COLORS.items():
        m = masks.get(name)
        if m is None or not np.asarray(m).any():
            continue
        if name == OTHER:
            continue
        img = _blend(img, m, color, alpha=alpha)
        pct = stats.get(name) if stats else None
        label = CLASS_LABELS.get(name, name)
        entries.append((f"{label}" + (f" — {pct * 100:.1f}%" if pct is not None else ""), color))
    return _save(_legend(img, entries), stem, out_dir)


def render_change_map(rgb_t1: np.ndarray, rgb_t2: np.ndarray, changed: np.ndarray, stem: str,
                      out_dir: Optional[Path] = None, clusters: Sequence[Region] = (),
                      changed_fraction: Optional[float] = None,
                      dates: Tuple[Optional[str], Optional[str]] = (None, None)) -> str:
    """Before / after / change-mask triptych -- the bi-temporal demo artefact."""
    a, b = _as_image(rgb_t1), _as_image(rgb_t2)
    if b.size != a.size:
        b = b.resize(a.size, Image.LANCZOS)
    overlay = _blend(b, changed, CHANGE_COLOR, alpha=0.5)
    overlay = _blend(overlay, _outline(np.asarray(changed, dtype=bool)), (255, 255, 0), alpha=0.9)
    dr = ImageDraw.Draw(overlay)
    width = max(2, overlay.width // 320)
    for r in clusters[:6]:
        if len(r.bbox_norm) != 4:
            continue
        x1 = r.bbox_norm[0] / 100.0 * overlay.width
        y1 = r.bbox_norm[1] / 100.0 * overlay.height
        x2 = r.bbox_norm[2] / 100.0 * overlay.width
        y2 = r.bbox_norm[3] / 100.0 * overlay.height
        dr.rectangle([x1, y1, x2, y2], outline=(255, 220, 0), width=width)

    gap = 8
    canvas = Image.new("RGB", (a.width * 3 + gap * 2, a.height + 18), (18, 20, 24))
    canvas.paste(a, (0, 18))
    canvas.paste(b, (a.width + gap, 18))
    canvas.paste(overlay, (2 * (a.width + gap), 18))
    dr = ImageDraw.Draw(canvas)
    d1 = dates[0] or "earlier"
    d2 = dates[1] or "later"
    dr.text((4, 4), f"t1 · {d1}", fill=(230, 230, 230))
    dr.text((a.width + gap + 4, 4), f"t2 · {d2}", fill=(230, 230, 230))
    pct = f" — {changed_fraction * 100:.1f}% of scene" if changed_fraction is not None else ""
    dr.text((2 * (a.width + gap) + 4, 4), f"change mask{pct}", fill=(255, 150, 150))
    return _save(_legend(canvas, [("changed area (yellow outline = cluster)", CHANGE_COLOR)]), stem, out_dir)


def render_fusion_map(rgb_optical: np.ndarray, masks: Dict[str, np.ndarray], stem: str,
                      out_dir: Optional[Path] = None,
                      stats: Optional[Dict[str, float]] = None,
                      obscured: Optional[np.ndarray] = None) -> str:
    """Joint optical+SAR class map, with the obscured area marked."""
    img = _as_image(rgb_optical)
    entries: List[Tuple[str, Tuple[int, int, int]]] = []
    for name in (WATER, BUILT_UP, VEGETATION):
        m = masks.get(name)
        if m is None or not np.asarray(m).any():
            continue
        img = _blend(img, m, CLASS_COLORS[name], alpha=0.5)
        pct = stats.get(name) if stats else None
        entries.append((f"joint {CLASS_LABELS.get(name, name)}" +
                        (f" — {pct * 100:.1f}%" if pct is not None else ""), CLASS_COLORS[name]))
    if obscured is not None and np.asarray(obscured).any():
        img = _blend(img, _outline(np.asarray(obscured, dtype=bool)), (255, 255, 255), alpha=0.9)
        entries.append(("optical obscured (SAR-informed)", (255, 255, 255)))
    return _save(_legend(img, entries), stem, out_dir)


def render_side_by_side(rgb_a: np.ndarray, rgb_b: np.ndarray, stem: str,
                        labels: Tuple[str, str] = ("optical", "SAR"),
                        out_dir: Optional[Path] = None) -> str:
    a, b = _as_image(rgb_a), _as_image(rgb_b)
    if b.size != a.size:
        b = b.resize(a.size, Image.LANCZOS)
    canvas = Image.new("RGB", (a.width * 2 + 8, a.height + 18), (18, 20, 24))
    canvas.paste(a, (0, 18))
    canvas.paste(b, (a.width + 8, 18))
    dr = ImageDraw.Draw(canvas)
    dr.text((4, 4), labels[0], fill=(230, 230, 230))
    dr.text((a.width + 12, 4), labels[1], fill=(230, 230, 230))
    return _save(canvas, stem, out_dir)
