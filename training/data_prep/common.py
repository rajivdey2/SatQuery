"""Shared helpers for dataset -> SFT-format converters.

Output format consumed by run_lora_train.py and the eval harness (VRSBench /
RSVQA / CDVQA) is one JSONL per stage: each line is
  {"messages":[{"role":"system","content":...},
               {"role":"user","content":<prompt>,"images":["relative/path"]},
               {"role":"assistant","content":<answer>}]}
`images` are relative to --data_dir (or absolute).
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import List, Tuple

SYSTEM_RS = (
    "You are SatQuery AI, a remote-sensing vision-language assistant for Indian Space "
    "Research Organisation (ISRO) satellite imagery. Answer concisely and only about "
    "what is visible in the image(s)."
)

GROUND_SYSTEM = SYSTEM_RS + " For object localisation use <box>(x1,y1,x2,y2)</box> with coordinates 0-100."


def die(msg: str):
    print(f"[fatal] {msg}", file=sys.stderr)
    sys.exit(1)


def load_meta_dir(meta_dir: Path, ext: str = ".json") -> List[Path]:
    if not meta_dir.exists():
        die(f"meta dir missing: {meta_dir}")
    return sorted(meta_dir.glob(f"*{ext}"))


def out_open(split_dir: Path, name: str):
    split_dir.mkdir(parents=True, exist_ok=True)
    return open(split_dir / f"{name}.jsonl", "w", encoding="utf-8")


def emit(handle, sys_msg: str, prompt: str, images: List[str], answer: str):
    record = {"messages": [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": prompt, "images": images},
        {"role": "assistant", "content": str(answer)},
    ]}
    handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def prepend_root(paths: List[str], root: str) -> List[str]:
    root = Path(root)
    out = []
    for p in paths:
        pp = Path(p)
        out.append(str(pp if pp.is_absolute() else root / pp))
    return out