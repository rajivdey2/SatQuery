"""RSVG / RSVGD (DIOR-based) -> SFT grounding records.

Referring-expression -> bounding-box pairs for the text-guided grounding task
(CLAUDE.md section 3). The official release is Pascal-VOC-style XML: one file per
image, each ``<object>`` carrying a ``<description>`` (the referring expression)
and a ``<bndbox>`` in absolute pixel coordinates.

Boxes are emitted in the 0..100 normalised space the rest of this system uses
(VRSBench convention), so one grounding format serves training, evaluation and the
audit trace.

Usage:
  python data_prep/rsvg_to_sft.py --root <rsvg_root> --out <out_dir>
  python data_prep/rsvg_to_sft.py --root <rsvg_root> --out <out_dir> --jsonl annotations.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import GROUND_SYSTEM, die, emit, out_open  # noqa: E402

PROMPT = 'Localise the object referred to by: "{expression}".'


def _norm_box(box: List[float], width: float, height: float) -> Optional[List[float]]:
    x1, y1, x2, y2 = (float(v) for v in box)
    if width <= 0 or height <= 0:
        return None
    if max(x1, y1, x2, y2) <= 1.0:                     # already 0..1
        x1, y1, x2, y2 = x1 * 100, y1 * 100, x2 * 100, y2 * 100
    elif max(x1, y1, x2, y2) > 100.0:                  # absolute pixels
        x1, y1 = x1 / width * 100, y1 / height * 100
        x2, y2 = x2 / width * 100, y2 / height * 100
    lo_x, hi_x = sorted((x1, x2))
    lo_y, hi_y = sorted((y1, y2))
    if hi_x - lo_x < 0.1 or hi_y - lo_y < 0.1:
        return None
    return [max(0.0, lo_x), max(0.0, lo_y), min(100.0, hi_x), min(100.0, hi_y)]


def _image_for(xml_path: Path, root: Path, declared: Optional[str]) -> Optional[Path]:
    stems = [declared] if declared else []
    stems += [f"{xml_path.stem}{ext}" for ext in (".jpg", ".png", ".jpeg", ".tif")]
    for stem in stems:
        if not stem:
            continue
        for base in (xml_path.parent, xml_path.parent.parent / "JPEGImages",
                     root / "JPEGImages", root / "images", root):
            candidate = base / Path(stem).name
            if candidate.exists():
                return candidate
    return None


def _from_xml(root: Path) -> Iterator[Tuple[Path, str, List[float], str]]:
    xmls = sorted(root.rglob("*.xml"))
    if not xmls:
        return
    print(f"[rsvg] {len(xmls)} XML annotation files")
    for xml_path in xmls:
        try:
            tree = ET.parse(xml_path)
        except ET.ParseError:
            continue
        node = tree.getroot()
        filename = (node.findtext("filename") or "").strip()
        size = node.find("size")
        width = float(size.findtext("width") or 0) if size is not None else 0.0
        height = float(size.findtext("height") or 0) if size is not None else 0.0
        image = _image_for(xml_path, root, filename)
        if image is None:
            continue
        if width <= 0 or height <= 0:
            try:
                from PIL import Image

                with Image.open(image) as im:
                    width, height = float(im.width), float(im.height)
            except Exception:
                continue
        split = _split_of(xml_path)
        for obj in node.findall("object"):
            expression = (obj.findtext("description") or obj.findtext("expression")
                          or obj.findtext("name") or "").strip()
            bnd = obj.find("bndbox")
            if not expression or bnd is None:
                continue
            try:
                box = [float(bnd.findtext(k)) for k in ("xmin", "ymin", "xmax", "ymax")]
            except (TypeError, ValueError):
                continue
            normalised = _norm_box(box, width, height)
            if normalised is None:
                continue
            yield image, expression, normalised, split


def _from_jsonl(path: Path, root: Path) -> Iterator[Tuple[Path, str, List[float], str]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        image = root / str(rec.get("image") or rec.get("filename") or "")
        expression = str(rec.get("expression") or rec.get("description") or "").strip()
        box = rec.get("box") or rec.get("bbox")
        if not image.exists() or not expression or not box:
            continue
        width = float(rec.get("width") or 0)
        height = float(rec.get("height") or 0)
        if width <= 0 or height <= 0:
            try:
                from PIL import Image

                with Image.open(image) as im:
                    width, height = float(im.width), float(im.height)
            except Exception:
                continue
        normalised = _norm_box(list(box), width, height)
        if normalised is None:
            continue
        yield image, expression, normalised, str(rec.get("split", "train")).lower()


def _split_of(path: Path) -> str:
    parts = {p.lower() for p in path.parts}
    for name in ("train", "val", "validation", "test"):
        if name in parts:
            return "val" if name in ("val", "validation") else name
    return "train"


def convert(root: Path, out: Path, jsonl: Optional[Path] = None, limit: int = 0) -> None:
    source = _from_jsonl(jsonl, root) if jsonl else _from_xml(root)
    splits: Dict[str, object] = {}
    n = 0
    for image, expression, box, split in source:
        if split not in splits:
            splits[split] = out_open(out / split, "rsvg")
        target = "<box>({:.2f},{:.2f},{:.2f},{:.2f})</box>".format(*box)
        emit(splits[split], GROUND_SYSTEM, PROMPT.format(expression=expression),
             [str(image)], target)
        n += 1
        if limit and n >= limit:
            break
    for handle in splits.values():
        handle.close()
    if not n:
        die(f"no grounding records produced from {root} "
            "(expected Pascal-VOC XML with <description> and <bndbox>, or --jsonl)")
    print(f"[rsvg] {n} grounding records -> {sorted(splits)} "
          "(boxes in 0..100 normalised coordinates)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--jsonl", default="", help="use a JSONL annotation file instead of XML")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    convert(Path(a.root), Path(a.out), Path(a.jsonl) if a.jsonl else None, limit=a.limit)
