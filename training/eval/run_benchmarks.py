"""Benchmark harness for the official eval targets (runs on a GPU box).

Standard metrics reported in README:
  RSVQA            : accuracy (exact match, categorical answers)
  CDVQA            : accuracy on test1 and test2 splits
  VRSBench         : A@0.5 / A@0.7 IoU for grounding (parsed <box> output),
                     GPT-judge agreement for VQA (see judge_scores.py)

Usage:
  python run_benchmarks.py --model Qwen/Qwen3-VL-4B-Instruct --adapter <dir> \
      --dataset rsvqa --root <rsvqa_root>            # multiple-choice acc
  python run_benchmarks.py --model ... --adapter ... \
      --dataset cdvqa --root <cdvqa_root>
  python run_benchmarks.py --model ... --adapter ... \
      --dataset vrsbench_ground --root <vrsbench_root> --ann <splits>.jsonl
"""
from __future__ import annotations

import argparse
import json
import pickle
import re
import sys
import time
from pathlib import Path
from typing import List

GROUND_SYS = ("You are a remote-sensing assistant. Localise the object and output exactly "
              "one <box>(x1,y1,x2,y2)</box> with coordinates 0-100.")
CHANGE_SYS = ("You are a remote-sensing change assistant. Image 1 is the earlier acquisition, "
              "Image 2 is the later acquisition. Answer only the change question.")
VQA_SYS = "You are a remote-sensing VQA assistant. Answer concisely."


def load_inference(model_name: str, adapter: str):
    import torch
    from peft import PeftModel
    from transformers import AutoModelForVision2Seq, AutoProcessor

    model = AutoModelForVision2Seq.from_pretrained(model_name, torch_dtype=torch.float16, device_map="auto")
    if adapter:
        model = PeftModel.from_pretrained(model, adapter)
    processor = AutoProcessor.from_pretrained(model_name)
    model.eval()
    return model, processor


def gen(model, processor, messages, images, max_tokens=64, temperature=0.0):
    import torch
    text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = processor(text=text, images=images, return_tensors="pt").to(model.device)
    out = model.generate(**inputs, max_new_tokens=max_tokens, do_sample=False)
    return processor.batch_decode(out[:, inputs["input_ids"].shape[-1]:], skip_special_tokens=True)[0].strip()


def iou(gt, pr):
    gx1, gy1, gx2, gy2 = gt
    px1, py1, px2, py2 = pr
    wx = max(0.0, min(gx2, px2) - max(gx1, px1))
    wy = max(0.0, min(gy2, py2) - max(gy1, py1))
    inter = wx * wy
    union = (gx2 - gx1) * (gy2 - gy1) + (px2 - px1) * (py2 - py1) - inter
    return inter / max(union, 1e-9)


def parse_box(text: str):
    m = re.search(r"<box>\s*\(([\d.]+),([\d.]+),([\d.]+),([\d.]+)\)", text)
    return [float(g) for g in m.groups()] if m else None


def rsvqa(model, processor, root: Path, max_n: int):
    from PIL import Image
    from sklearn.metrics import accuracy_score

    # RSVQA official release ships pickled DataFrames (rsvqa_lr_test.pickle)
    # with per-question rows; accept that, or the converter-style CSV below.
    pic = list(root.glob("rsvqa_*test*.pickle"))
    if not pic:
        pic = list(root.glob("*.pickle"))
    if pic:
        import pandas as pd

        df = pickle.load(pic[0].open("rb"))
    else:
        import pandas as pd

        csv = root / "rsvqa_benchmark.csv"
        if not csv.exists():
            sys.exit("provide RSVQA as *.pickle (official) or a rsvqa_benchmark.csv with image/question/answer columns")
        df = pd.read_csv(csv)
    ans_col = [c for c in df.columns if c.lower() in ("answer", "label")][0]
    q_col = [c for c in df.columns if c.lower() in ("question", "query")][0]
    img_col = [c for c in df.columns if c.lower() in ("image_id", "image", "filename")][0]
    y_true, y_pred = [], []
    for _, row in df.iterrows():
        if len(y_true) >= max_n:
            break
        p = root / str(row[img_col]).split("/")[-1]
        if not p.exists():
            # RSVQA images may live in the root next to the pickle
            p = root / str(row[img_col])
        if not p.exists():
            continue
        img = Image.open(p).convert("RGB")
        messages = [{"role": "system", "content": VQA_SYS},
                    {"role": "user", "content": f"Answer the question about this remote-sensing image. Question: {row[q_col]}"}]
        pred = gen(model, processor, messages, [img])
        y_true.append(str(row[ans_col]).strip().lower())
        y_pred.append(pred.strip().lower())
    acc = accuracy_score(y_true, y_pred)
    return {"dataset": "RSVQA", "n": len(y_true), "accuracy": round(acc, 4), "sample_preds": y_pred[:3]}, y_true, y_pred


def cdvqa(model, processor, root: Path, max_n: int):
    from PIL import Image
    from sklearn.metrics import accuracy_score

    ann = root / "cdvqa_qa.json"
    recs = json.loads(ann.read_text(encoding="utf-8"))
    per = {}
    for r in recs:
        split = r.get("split", "test")
        if len(per.setdefault(split, [])) >= max_n:
            continue
        i1, i2 = root / r["img1"], root / r["img2"]
        if not i1.exists() or not i2.exists():
            continue
        options = r.get("options") or ["not changed"]
        prompt = ("Answer the change question with the option text. Options:\n" +
                  "\n".join(f"{chr(65 + i)}. {o}" for i, o in enumerate(options)) +
                  f"\nQuestion: {r['question']}")
        images = [Image.open(i1).convert("RGB"), Image.open(i2).convert("RGB")]
        pred = gen(model, processor, [{"role": "system", "content": CHANGE_SYS},
                                      {"role": "user", "content": prompt}], images, max_tokens=48)
        gt = options[r["answer"]] if r["answer"] is not None else ""
        per[split].append((round(accuracy_score([gt], [pred])), gt, pred))
    out = {k: {"n": len(v), "accuracy": round(sum(x[0] for x in v) / max(len(v), 1), 4)} for k, v in per.items()}
    return {"dataset": "CDVQA", "splits": out}, None, None


def vrsbench_ground(model, processor, root: Path, ann_jsonl: Path, max_n: int):
    from PIL import Image

    hits = {0.5: 0, 0.7: 0}
    n = 0
    with ann_jsonl.open(encoding="utf-8") as fh:
        for line in fh:
            if n >= max_n:
                break
            rec = json.loads(line)
            if "box" not in rec:
                continue
            p = root / rec["image"]
            if not p.exists():
                continue
            img = Image.open(p).convert("RGB")
            msg = [{"role": "system", "content": GROUND_SYS},
                   {"role": "user", "content": f"Localise the object referred to by: \"{rec['expression']}\"."}]
            pred = parse_box(gen(model, processor, msg, [img]))
            if not pred:
                continue
            gt = [float(x) * 100 for x in rec["box"]] if max(rec["box"]) <= 1 else [float(x) for x in rec["box"]]
            if max(gt) <= 1:
                gt = [x * 100 for x in gt]
            score = iou(gt, pred)
            if score >= 0.5:
                hits[0.5] += 1
            if score >= 0.7:
                hits[0.7] += 1
            n += 1
    return {"dataset": "VRSBench-grounding", "n": n,
            "A@0.5": round(hits[0.5] / max(n, 1), 4), "A@0.7": round(hits[0.7] / max(n, 1), 4)}, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True, choices=["rsvqa", "cdvqa", "vrsbench_ground"])
    ap.add_argument("--root", required=True)
    ap.add_argument("--model", default="Qwen/Qwen3-VL-4B-Instruct")
    ap.add_argument("--adapter", default="")
    ap.add_argument("--max-n", type=int, default=2000)
    ap.add_argument("--ann", default="", help="vrsbench grounding jsonl")
    a = ap.parse_args()
    model, processor = load_inference(a.model, a.adapter)
    t0 = time.time()
    if a.dataset == "rsvqa":
        result, *_ = rsvqa(model, processor, Path(a.root), a.max_n)
    elif a.dataset == "cdvqa":
        result, *_ = cdvqa(model, processor, Path(a.root), a.max_n)
    else:
        result, *_ = vrsbench_ground(model, processor, Path(a.root), Path(a.ann), a.max_n)
    result["elapsed_s"] = round(time.time() - t0, 1)
    result["adapter"] = a.adapter or "none"
    out = Path(__file__).parent / "results" / f"{a.dataset}_{int(time.time())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    print("saved ->", out)


if __name__ == "__main__":
    main()