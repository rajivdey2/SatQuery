"""VRSBench -> SFT records (VQA, captioning, and referring-expression grounding).

Official source: vrsbench.github.io (release contains images + annotations
covering VQA, region captioning, and region grounding with COCO-style boxes in
relative coordinates; grounding boxes are given in 0..100 space).

Supported annotation JSONL per line:
  {image, question, answer}                                   -> VQA
  {image, caption} / {image, desc}                            -> caption
  {image, expression, box:[x1,y1,x2,y2]} (0..100 or 0..1)     -> grounding

Usage:
  python data_prep/vrsbench_to_sft.py --root <release_dir> --ann annotations.jsonl --out <out_dir>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import GROUND_SYSTEM, SYSTEM_RS, die, emit, out_open, prepend_root


def _box100(box: List[float]) -> str:
    x1, y1, x2, y2 = map(float, box)
    if max(x1, y1, x2, y2) <= 1.0:  # normalize 0..1 -> 0..100
        x1, y1, x2, y2 = x1 * 100, y1 * 100, x2 * 100, y2 * 100
    return f"({x1:.2f},{y1:.2f},{x2:.2f},{y2:.2f})"


def convert(root: Path, ann: Path, out: Path) -> None:
    if not ann.exists():
        die(f"annotations missing: {ann}")
    splits = {k: out_open(out / k, "vrsbench") for k in ("train", "val", "test")}
    n = {"vqa": 0, "caption": 0, "grounding": 0}
    for line in ann.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        im = root / rec["image"]
        if not im.exists():
            continue
        split = rec.get("split", "train")
        if split not in splits:
            continue
        h = splits[split]
        if "box" in rec:  # grounding
            nl = "<box>%s</box>" % _box100(rec["box"])
            emit(h, GROUND_SYSTEM, f"Localise the object referred to by: \"{rec['expression']}\".", [str(im)], nl)
            n["grounding"] += 1
        elif "question" in rec:  # VQA
            emit(h, SYSTEM_RS, f"Answer the question about this remote-sensing image.\nQuestion: {rec['question']}",
                 [str(im)], rec["answer"])
            n["vqa"] += 1
        else:  # caption
            emit(h, SYSTEM_RS,
                 "Describe the land-cover and the major objects visible in this remote-sensing image.",
                 [str(im)], rec.get("caption") or rec.get("desc"))
            n["caption"] += 1
    for s in splits.values():
        s.close()
    print(f"vrsbench: {n} written to {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--ann", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    convert(Path(a.root), Path(a.ann), Path(a.out))