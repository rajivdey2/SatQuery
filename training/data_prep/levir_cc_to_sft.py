"""LEVIR-CC -> SFT change-description records.

LEVIR-CC pairs 10,077 bi-temporal image pairs with free-text change captions
(five per pair), which is what teaches a model *what* changed and *where* rather
than only whether something changed (CLAUDE.md section 5). LEVIR-CD / LEVIR-MCI
ship the corresponding pixel masks; when a mask directory is present its measured
change fraction is appended to the target text so the model learns to state an
extent rather than a vague adjective.

Expected layout (the official release, or anything close to it):
  <root>/images/train/A/<id>.png      earlier acquisition
  <root>/images/train/B/<id>.png      later acquisition
  <root>/images/train/label/<id>.png  optional change mask (LEVIR-CD/MCI)
  <root>/LevirCCcaptions.json         {"images":[{"filename","split","sentences":[{"raw"}...]}]}

Usage:
  python data_prep/levir_cc_to_sft.py --root <release_dir> --out <out_dir>
  python data_prep/levir_cc_to_sft.py --root <release_dir> --out <out_dir> --with-mask-extent
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import SYSTEM_RS, die, emit, out_open  # noqa: E402

CHANGE_SYS = (SYSTEM_RS + " Image 1 is the earlier acquisition, Image 2 is the later acquisition "
              "of the same area.")
PROMPT = ("Describe what changed between the two acquisitions and where the change occurred.")


def _load_captions(root: Path) -> Dict[str, Tuple[str, List[str]]]:
    """filename -> (split, captions)."""
    candidates = (list(root.glob("*aptions*.json")) + list(root.glob("*.json")))
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        images = payload.get("images") if isinstance(payload, dict) else None
        if not images:
            continue
        out: Dict[str, Tuple[str, List[str]]] = {}
        for entry in images:
            name = entry.get("filename") or entry.get("file_name")
            if not name:
                continue
            sentences = entry.get("sentences") or []
            captions = [s.get("raw") or " ".join(s.get("tokens", [])) for s in sentences
                        if isinstance(s, dict)]
            captions = [c.strip() for c in captions if c and c.strip()]
            if not captions:
                continue
            out[name] = (str(entry.get("split", "train")).lower(), captions)
        if out:
            print(f"[levir-cc] captions read from {path.name}: {len(out)} pairs")
            return out
    die(f"no caption JSON with an 'images' array found under {root}")
    return {}


def _find_pair(root: Path, split: str, name: str) -> Optional[Tuple[Path, Path, Optional[Path]]]:
    for base in (root / "images" / split, root / split, root / "images"):
        a, b = base / "A" / name, base / "B" / name
        if a.exists() and b.exists():
            mask = None
            for label_dir in ("label", "labels", "OUT", "mask"):
                candidate = base / label_dir / name
                if candidate.exists():
                    mask = candidate
                    break
            return a, b, mask
    return None


def _mask_extent(path: Path) -> Optional[float]:
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as im:
            arr = np.asarray(im.convert("L"))
        return float((arr > 127).mean())
    except Exception:
        return None


def convert(root: Path, out: Path, with_mask_extent: bool = False, limit: int = 0,
            captions_per_pair: int = 1) -> None:
    captions = _load_captions(root)
    splits: Dict[str, object] = {}
    counts = {"records": 0, "pairs": 0, "with_mask": 0, "missing": 0}

    for name, (split, sentences) in sorted(captions.items()):
        split = {"validation": "val", "valid": "val"}.get(split, split)
        found = _find_pair(root, split, name)
        if found is None:
            counts["missing"] += 1
            continue
        a, b, mask = found
        if split not in splits:
            splits[split] = out_open(out / split, "levir_cc")
        counts["pairs"] += 1

        extent_note = ""
        if with_mask_extent and mask is not None:
            fraction = _mask_extent(mask)
            if fraction is not None:
                counts["with_mask"] += 1
                extent_note = f" The changed area covers {fraction * 100:.1f}% of the scene."

        for caption in sentences[: max(1, captions_per_pair)]:
            target = caption.rstrip(".") + "." + extent_note
            emit(splits[split], CHANGE_SYS, PROMPT, [str(a), str(b)], target)
            counts["records"] += 1
        if limit and counts["pairs"] >= limit:
            break

    for handle in splits.values():
        handle.close()
    print(f"[levir-cc] {counts['records']} records from {counts['pairs']} pairs "
          f"-> {sorted(splits)}")
    if counts["with_mask"]:
        print(f"[levir-cc] {counts['with_mask']} pairs carried a change mask; measured extent "
              "appended to the target text")
    if counts["missing"]:
        print(f"[levir-cc] {counts['missing']} captioned pairs had no matching A/B images")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--with-mask-extent", action="store_true",
                    help="append the measured change fraction from the LEVIR-CD/MCI mask")
    ap.add_argument("--captions-per-pair", type=int, default=1,
                    help="LEVIR-CC ships five captions per pair; 1 keeps the set balanced")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    convert(Path(a.root), Path(a.out), with_mask_extent=a.with_mask_extent,
            limit=a.limit, captions_per_pair=a.captions_per_pair)
