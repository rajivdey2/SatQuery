"""BigEarthNet-MM (S1+S2 pairs) -> SFT fusion records.

BigEarthNet v1/v2 (reBEN) provides co-registered Sentinel-1 (SAR, VV/VH dB) and
Sentinel-2 (optical, 10m RGB) patches with CORINE land-cover multi-labels. The
task "use both images" is trained VQA-style against the CORINE labels as weak
supervision. See CLAUDE.md section 6 for the two-stage plan; this is the
supervised stage.

Expected layout (from the official download):
  <root>/patches/<patch>.tif          (multi-band S2 raster)
  <root>/s1/<patch>_S1.tif            (S1 VV/VH, dB)
  <root>/labels.jsonl                [{"patch":..., "labels":["Agricultural", ...]}]
Usage:
  python data_prep/ben_mm_to_sft.py --root <root> --out <out_dir>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import SYSTEM_RS, die, emit, out_open

FUSION_SYS = SYSTEM_RS + " Image 1 is optical, Image 2 is SAR of the same area."


def convert(root: Path, out: Path) -> None:
    labels_path = root / "labels.jsonl"
    if not labels_path.exists():
        die(f"expected {labels_path}")
    splits = {k: out_open(out / k, "benmm") for k in ("train", "val", "test")}
    n = 0
    for line in labels_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        patch = rec["patch"]
        s2 = root / "patches" / f"{patch}.tif"
        s1 = root / "s1" / f"{patch}_S1.tif"
        if not s2.exists() or not s1.exists():
            continue
        split = rec.get("split", "train")
        labels = rec.get("labels") or []
        if not labels:
            continue
        q = "Using both the optical and the SAR image together, which land-cover classes are present in this area? Answer with the class list."
        emit(splits[split], FUSION_SYS, q, [str(s2), str(s1)], ", ".join(labels))
        n += 1
    for s in splits.values():
        s.close()
    print(f"benmm: {n} fusion records -> {sorted(splits)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    convert(Path(a.root), Path(a.out))