"""CDVQA -> SFT change-VQA records (also the eval harness input).

CDVQA (Yuan et al. 2021) is built on the SECOND dataset: bi-temporal image
pairs, multi-choice change questions with 6 land-cover change classes plus a
'no change' option. The PS names CDVQA as the change-VQA eval target.

DOCTYPE expected (adapt to the exact annotation you obtain):
  --root/cdvqa_qa.json   [{"img1":"a/2012.tif","img2":"a/2016.tif",
                            "question":"...","options":["..."],"answer":2,"split":"test1"}]
Usage:
  python data_prep/cdvqa_to_sft.py --root <root> --ann cdvqa_qa.json --out <out_dir>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import SYSTEM_RS, die, emit, out_open

CHANGE_SYS = SYSTEM_RS + " Image 1 is the earlier acquisition, Image 2 is the later acquisition."


def convert(root: Path, ann: Path, out: Path) -> None:
    if not ann.exists():
        die(f"annotations missing: {ann}")
    recs = json.loads(ann.read_text(encoding="utf-8"))
    splits = {}
    for r in recs:
        split = r.get("split", "test")
        if split not in splits:
            splits[split] = out_open(out / split, "cdvqa")
        i1, i2 = root / r["img1"], root / r["img2"]
        if not i1.exists() or not i2.exists():
            continue
        options = r.get("options") or []
        prompt = (f"Answer the change question. Options:\n" +
                  "\n".join(f"{chr(65 + i)}. {o}" for i, o in enumerate(options)) +
                  f"\nQuestion: {r['question']}")
        answer = options[r["answer"]] if r.get("answer") is not None and r["answer"] < len(options) else (options or ["none"])[0]
        emit(splits[split], CHANGE_SYS, prompt, [str(i1), str(i2)], answer)
    for s in splits.values():
        s.close()
    print(f"cdvqa: {sum(1 for r in recs)} records -> {sorted(splits)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--ann", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    convert(Path(a.root), Path(a.ann), Path(a.out))