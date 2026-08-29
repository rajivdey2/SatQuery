"""GPT-style local judge for VRSBench VQA / caption scores (no API needed).

VRSBench's official evaluation uses a GPT judge; since we cannot call APIs from
the laptop/Colab reliably, this judges with a local open model (default
Qwen3-8B) against the reference answer and returns an agreement rate.

Usage:
  python judge_scores.py --model Qwen/Qwen3-8B \
      --preds results/vrsbench_vqa_predictions.jsonl \
      --out results/vrsbench_vqa_judged.json

--preds format per line: {"reference": "...", "prediction": "..."}
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

JUDGE_PROMPT = (
    "You are grading a remote-sensing vision-language answer. "
    "Question-independent grading: compare the [Reference] answer and the [Prediction] answer. "
    "Reply ONLY with YES if the prediction conveys the same information (correct, or a "
    "reasonable equivalent), or NO if it is wrong or hallucinated.\n"
    "[Reference]: {ref}\n[Prediction]: {pred}\nAnswer (YES/NO):"
)


def load_judge(model_name: str):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(model_name)
    m = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype=torch.float16, device_map="auto")
    m.eval()
    return m, tok


def judge(model, tok, ref: str, pred: str) -> bool:
    prompt = JUDGE_PROMPT.format(ref=ref, pred=pred)
    inputs = tok(prompt, return_tensors="pt").to(model.device)
    out = model.generate(**inputs, max_new_tokens=8, do_sample=False)
    ans = tok.batch_decode(out[:, inputs["input_ids"].shape[-1]:], skip_special_tokens=True)[0].strip().upper()
    return ans.startswith("YES")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen3-8B")
    ap.add_argument("--preds", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-n", type=int, default=1500)
    a = ap.parse_args()
    rows = [json.loads(l) for l in Path(a.preds).read_text(encoding="utf-8").splitlines() if l.strip()][: a.max_n]
    model, tok = load_judge(a.model)
    agree = 0
    t0 = time.time()
    for i, r in enumerate(rows):
        if judge(model, tok, r["reference"], r["prediction"]):
            agree += 1
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(rows)} agree={agree}")
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    report = {"n": len(rows), "judge_agreement": round(agree / max(len(rows), 1), 4),
              "judge_model": a.model, "elapsed_s": round(time.time() - t0, 1)}
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()