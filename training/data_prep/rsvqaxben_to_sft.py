"""RSVQAxBEN -> SFT VQA records.

RSVQAxBEN is VQA pairs generated directly on BigEarthNet patches -- it is the
bridging set that satisfies the "fine-tuned using BigEarthNet" requirement in
VQA-task form. It also acts as the change/fusion pretraining sanity set.

Zenodo downloads:
  * RSVQAxBEN zips (see docs/PPT_CONTEXT.md / rsvqa.sylvainlobry.com). The
    archive contains BigEarthNet patch images (.tif) plus one JSON per patch
    with fields {"question", "answers", "split"} (split in train|val|test).

The converter accepts any nesting for images (.tif) and meta (.json); patches
are matched to their JSON by filename stem.

Usage:
  python data_prep/rsvqaxben_to_sft.py --root <unzipped_rsvqaxben> --out <out_dir>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import SYSTEM_RS, die, emit, out_open  # noqa: E402


def convert(root: Path, out: Path) -> None:
    def _rfind(*exts: str):
        return sorted((p for p in root.rglob("*") if p.suffix.lower() in exts and "README" not in p.name))

    jsons = _rfind(".json")
    if not jsons:
        die(f"no *.json metadata found under {root}")
    tifs = {p.stem: p for p in _rfind(".tif", ".tiff")}
    print(f"[rsvqaxben] {len(jsons)} meta JSONs, {len(tifs)} patch images")

    splits = {k: out_open(out / k, "rsvqaxben") for k in ("train", "val", "test")}
    n = 0
    for meta in jsons:
        data = json.loads(meta.read_text(encoding="utf-8"))
        split = data.get("split", "train")
        if split not in splits:
            continue
        patch_id = data.get("patch") or meta.stem
        img = tifs.get(patch_id) or tifs.get(meta.stem)
        if img is None:
            continue
        question = data.get("question") or data.get("query") or ""
        answers = data.get("answers") or data.get("answer") or ["unknown"]
        answer = answers[0] if isinstance(answers, list) else answers
        if not question or not answer:
            continue
        emit(splits[split], SYSTEM_RS,
             f"Answer the question about this remote-sensing image.\nQuestion: {question}",
             [str(img)], answer)
        n += 1
    for h in splits.values():
        h.close()
    # drop empty split files
    for k in list(splits):
        p = out / k / "rsvqaxben.jsonl"
        if p.stat().st_size == 0:
            p.unlink()
            splits.pop(k)
    print(f"[rsvqaxben] {n} records -> {sorted(splits)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    convert(Path(a.root), Path(a.out))