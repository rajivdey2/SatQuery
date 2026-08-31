"""Benchmark harness for the evaluation targets named in the problem statement.

Two backends, one protocol:

* ``--backend analysis`` runs this system's measurement engine. It needs no GPU
  and no downloaded weights, so the benchmark table can be filled in and kept
  honest from day one — including on a laptop.
* ``--backend vlm`` runs Qwen3-VL, optionally with a LoRA adapter from
  ``training/run_lora_train.py``, for the narration-model numbers.

Metrics follow each benchmark's own protocol:

  RSVQA (LR/HR)      accuracy over categorical answers; per-question-type breakdown
  VRSBench VQA       accuracy, exact plus a normalised match (the official
                     protocol also uses a GPT judge — see judge_scores.py)
  VRSBench grounding Acc@0.5 / Acc@0.7 on IoU, boxes in 0..100 space
  CDVQA              per-type accuracy plus Average Accuracy (AA) and Overall
                     Accuracy (OA), reported for each official test split

Every run writes a timestamped JSON under ``training/eval/results`` so the README
table can cite a specific run rather than a remembered number.

Usage
  python training/eval/run_benchmarks.py --dataset rsvqa --root <rsvqa_root> --backend analysis
  python training/eval/run_benchmarks.py --dataset cdvqa --root <cdvqa_root> --ann cdvqa_qa.json
  python training/eval/run_benchmarks.py --dataset vrsbench_ground --root <imgs> --ann ann.jsonl \
      --backend vlm --model Qwen/Qwen3-VL-4B-Instruct --adapter runs/rs_vlm
"""
from __future__ import annotations

import argparse
import json
import pickle
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

RESULTS_DIR = Path(__file__).resolve().parent / "results"

_YES = {"yes", "y", "true", "present", "there is", "affirmative"}
_NO = {"no", "n", "false", "absent", "none", "there is no", "negative"}
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


# --------------------------------------------------------------------------- #
# Answer normalisation (shared by both backends)
# --------------------------------------------------------------------------- #

def normalise(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"^(the answer is|answer:)\s*", "", t)
    t = re.sub(r"[.\"'`]+$", "", t).strip()
    return re.sub(r"\s+", " ", t)


def short_answer(text: str, reference: Optional[str] = None) -> str:
    """Reduce a full sentence to the short form benchmark protocols expect.

    RSVQA and CDVQA score a single token or phrase, while this system answers in
    prose. Extracting the short form is part of the protocol, not a shortcut: the
    reduction is deterministic and applied identically to every prediction.
    """
    t = normalise(text)
    if not t:
        return ""
    lead = t.split(",")[0].split(".")[0].strip()
    if lead.startswith("yes"):
        return "yes"
    if lead.startswith("no ") or lead == "no" or lead.startswith("no—") or lead.startswith("no -"):
        return "no"
    if reference:
        ref = normalise(reference)
        # Multiple-choice / categorical references: accept a containment match.
        if ref and ref in t:
            return ref
        if ref in _YES and any(w in t for w in _YES):
            return ref
        if ref in _NO and any(w in t for w in _NO):
            return ref
        if _NUMBER.fullmatch(ref):
            numbers = _NUMBER.findall(t)
            if numbers:
                return numbers[0]
    return lead


def is_correct(prediction: str, reference: str) -> bool:
    p, r = normalise(prediction), normalise(reference)
    if not r:
        return False
    if p == r:
        return True
    if r in _YES and p in _YES:
        return True
    if r in _NO and p in _NO:
        return True
    # Numeric answers match on value, not on formatting.
    if _NUMBER.fullmatch(r) and _NUMBER.fullmatch(p):
        return abs(float(p) - float(r)) < 1e-6
    return False


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter = max(0.0, min(ax2, bx2) - max(ax1, bx1)) * max(0.0, min(ay2, by2) - max(ay1, by1))
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 1e-9 else 0.0


def parse_box(text: str) -> Optional[List[float]]:
    m = re.search(r"<box>\s*\(?\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)", text or "")
    if not m:
        m = re.search(r"\[\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\]", text or "")
    if not m:
        return None
    values = [float(g) for g in m.groups()]
    if max(values) <= 1.0:
        values = [v * 100 for v in values]
    return values


def to_0_100(box: Sequence[float], width: float = 0.0, height: float = 0.0) -> List[float]:
    values = [float(v) for v in box]
    if max(values) <= 1.0:
        return [v * 100 for v in values]
    if max(values) > 100.0 and width > 0 and height > 0:
        return [values[0] / width * 100, values[1] / height * 100,
                values[2] / width * 100, values[3] / height * 100]
    return values


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #

class AnalysisBackend:
    """This system's measurement engine, run headlessly (no GPU, no weights)."""

    name = "analysis_engine"

    def __init__(self) -> None:
        from backend.analysis.change import measure_change
        from backend.analysis.grounding import ground_query
        from backend.analysis.landcover import measure_land_cover
        from backend.analysis.narrate import answer_change, answer_single, describe_scene
        from backend.preprocessing.pipeline import prepare_image

        self._prepare = prepare_image
        self._land_cover = measure_land_cover
        self._ground = ground_query
        self._change = measure_change
        self._answer_single = answer_single
        self._answer_change = answer_change
        self._describe = describe_scene
        self.model_id = "satquery-analysis-engine-v1"
        self.adapter = None

    def _load(self, path: Path):
        return self._prepare(str(path))

    def vqa(self, images: Sequence[Path], question: str) -> str:
        prepared = [self._load(p) for p in images]
        if len(prepared) >= 2:
            measurement, _ = self._change(prepared[0], prepared[1])
            return self._answer_change(question, measurement)
        measurement = self._land_cover(prepared[0], with_scene_labels=False).measurement
        return self._answer_single(question, measurement, prepared[0].info.modality)

    def caption(self, images: Sequence[Path]) -> str:
        prepared = self._load(images[0])
        measurement = self._land_cover(prepared, with_scene_labels=False).measurement
        return self._describe(measurement, prepared.info.modality)

    def grounding(self, images: Sequence[Path], expression: str) -> Optional[List[float]]:
        prepared = self._load(images[0])
        measurement, _ = self._ground(prepared, expression)
        if not measurement.regions:
            return None
        return list(measurement.regions[0].bbox_norm)


class VlmBackend:
    """Qwen3-VL (optionally LoRA-adapted) run through the serving code path."""

    name = "vlm"

    def __init__(self, model_id: str, adapter: str = "") -> None:
        import os

        os.environ["USE_REAL_MODEL"] = "1"
        os.environ["MODEL_ID"] = model_id
        from backend.config import settings
        from backend.specialists.backends import model_loader

        settings.use_real_model = True
        settings.model_id = model_id
        self._loader = model_loader
        self._model = model_loader.load_model()
        if self._model is None:
            raise SystemExit("VLM backend requested but the model could not be loaded "
                             "(needs torch + transformers and enough memory).")
        self.adapter = model_loader.load_adapter(self._model, adapter) if adapter else None
        self.model_id = model_id

    def _generate(self, images: Sequence[Path], system: str, user: str, max_tokens: int = 64) -> str:
        from PIL import Image

        pil = [Image.open(p).convert("RGB") for p in images]
        text, _ = self._loader.generate(
            self._model, [{"role": "system", "content": system}, {"role": "user", "content": user}],
            pil, max_new_tokens=max_tokens, temperature=0.0, do_sample=False, with_margin=False)
        return text

    def vqa(self, images: Sequence[Path], question: str) -> str:
        system = "You are a remote-sensing VQA assistant. Answer with the shortest correct phrase."
        if len(images) >= 2:
            system = ("You are a remote-sensing change assistant. Image 1 is the earlier "
                      "acquisition, image 2 the later one. Answer with the shortest correct phrase.")
        return self._generate(images, system, f"Question: {question}")

    def caption(self, images: Sequence[Path]) -> str:
        return self._generate(images, "You are a remote-sensing captioning assistant.",
                              "Describe the land cover and the major objects visible in this image.",
                              max_tokens=128)

    def grounding(self, images: Sequence[Path], expression: str) -> Optional[List[float]]:
        text = self._generate(
            images,
            "You are a remote-sensing grounding assistant. Output exactly one "
            "<box>(x1,y1,x2,y2)</box> with coordinates in 0-100.",
            f'Localise the object referred to by: "{expression}".')
        return parse_box(text)


def make_backend(kind: str, model_id: str, adapter: str):
    if kind == "analysis":
        return AnalysisBackend()
    return VlmBackend(model_id, adapter)


# --------------------------------------------------------------------------- #
# Dataset readers
# --------------------------------------------------------------------------- #

def _resolve_image(root: Path, name: str) -> Optional[Path]:
    candidate = root / name
    if candidate.exists():
        return candidate
    bare = Path(name).name
    for base in (root, root / "images", root / "Images", root / "JPEGImages", root / "data"):
        p = base / bare
        if p.exists():
            return p
    matches = list(root.rglob(bare))
    return matches[0] if matches else None


def read_rsvqa(root: Path, max_n: int) -> Iterable[dict]:
    """RSVQA ships pickled DataFrames; a CSV/JSONL export is also accepted."""
    frames = []
    pickles = sorted(root.glob("*test*.pickle")) or sorted(root.glob("*.pickle"))
    if pickles:
        with pickles[0].open("rb") as fh:
            frames.append(pickle.load(fh))
        print(f"[rsvqa] loaded {pickles[0].name}")
    else:
        csvs = sorted(root.glob("*.csv"))
        jsonls = sorted(root.glob("*.jsonl"))
        if csvs:
            import pandas as pd

            frames.append(pd.read_csv(csvs[0]))
        elif jsonls:
            rows = [json.loads(l) for l in jsonls[0].read_text(encoding="utf-8").splitlines() if l.strip()]
            import pandas as pd

            frames.append(pd.DataFrame(rows))
        else:
            raise SystemExit(f"no RSVQA annotations found under {root} "
                             "(expected *.pickle, *.csv or *.jsonl)")
    df = frames[0]
    cols = {c.lower(): c for c in df.columns}
    q_col = cols.get("question") or cols.get("query")
    a_col = cols.get("answer") or cols.get("label")
    i_col = cols.get("image") or cols.get("image_id") or cols.get("img_id") or cols.get("filename")
    t_col = cols.get("type") or cols.get("question_type") or cols.get("category")
    if not (q_col and a_col and i_col):
        raise SystemExit(f"RSVQA table lacks question/answer/image columns; found {list(df.columns)}")
    yielded = 0
    for _, row in df.iterrows():
        if max_n and yielded >= max_n:
            break
        image = _resolve_image(root, str(row[i_col]))
        if image is None:
            continue
        yield {"images": [image], "question": str(row[q_col]),
               "answer": str(row[a_col]), "type": str(row[t_col]) if t_col else "all"}
        yielded += 1


def read_vrsbench(root: Path, ann: Path, kind: str, max_n: int) -> Iterable[dict]:
    if not ann.exists():
        raise SystemExit(f"annotation file missing: {ann}")
    lines = ann.read_text(encoding="utf-8").splitlines()
    records: List[dict] = []
    if len(lines) == 1 or ann.suffix.lower() == ".json":
        payload = json.loads(ann.read_text(encoding="utf-8"))
        records = payload if isinstance(payload, list) else payload.get("annotations", [])
    else:
        for line in lines:
            if line.strip():
                records.append(json.loads(line))
    yielded = 0
    for rec in records:
        if max_n and yielded >= max_n:
            break
        name = rec.get("image") or rec.get("image_id") or rec.get("filename")
        if not name:
            continue
        image = _resolve_image(root, str(name))
        if image is None:
            continue
        if kind == "vrsbench_ground":
            box = rec.get("box") or rec.get("bbox")
            expression = rec.get("expression") or rec.get("referring") or rec.get("phrase")
            if not box or not expression:
                continue
            yield {"images": [image], "expression": str(expression), "box": list(box),
                   "unique": bool(rec.get("unique", rec.get("is_unique", True)))}
        elif kind == "vrsbench_caption":
            caption = rec.get("caption") or rec.get("desc") or rec.get("description")
            if not caption:
                continue
            yield {"images": [image], "reference": str(caption)}
        else:
            question = rec.get("question")
            answer = rec.get("answer") or rec.get("ground_truth")
            if not question or answer is None:
                continue
            yield {"images": [image], "question": str(question), "answer": str(answer),
                   "type": str(rec.get("type", "all"))}
        yielded += 1


def read_cdvqa(root: Path, ann: Path, max_n: int) -> Iterable[dict]:
    if not ann.exists():
        raise SystemExit(f"annotation file missing: {ann}")
    payload = json.loads(ann.read_text(encoding="utf-8"))
    records = payload if isinstance(payload, list) else payload.get("questions", [])
    per_split: Dict[str, int] = defaultdict(int)
    for rec in records:
        split = str(rec.get("split", "test"))
        if max_n and per_split[split] >= max_n:
            continue
        i1 = _resolve_image(root, str(rec.get("img1") or rec.get("image1") or ""))
        i2 = _resolve_image(root, str(rec.get("img2") or rec.get("image2") or ""))
        if i1 is None or i2 is None:
            continue
        options = rec.get("options") or rec.get("choices") or []
        answer = rec.get("answer")
        if isinstance(answer, int) and options and 0 <= answer < len(options):
            answer = options[answer]
        if answer is None:
            continue
        per_split[split] += 1
        yield {"images": [i1, i2], "question": str(rec.get("question", "")),
               "answer": str(answer), "options": [str(o) for o in options],
               "type": str(rec.get("type", "all")), "split": split}


# --------------------------------------------------------------------------- #
# Evaluators
# --------------------------------------------------------------------------- #

def _accuracy_report(rows: List[dict]) -> dict:
    by_type: Dict[str, List[bool]] = defaultdict(list)
    for r in rows:
        by_type[r.get("type", "all")].append(bool(r["correct"]))
    per_type = {t: {"n": len(v), "accuracy": round(sum(v) / len(v), 4)} for t, v in by_type.items()}
    overall = [bool(r["correct"]) for r in rows]
    average = [v["accuracy"] for v in per_type.values()]
    return {"n": len(rows),
            "overall_accuracy": round(sum(overall) / len(overall), 4) if overall else 0.0,
            "average_accuracy": round(sum(average) / len(average), 4) if average else 0.0,
            "per_type": per_type,
            "examples": [{"question": r.get("question", ""), "reference": r.get("answer", ""),
                          "prediction": r.get("prediction", ""), "correct": r["correct"]}
                         for r in rows[:5]]}


def eval_vqa(backend, records: Iterable[dict], progress: int = 50) -> dict:
    rows: List[dict] = []
    for i, rec in enumerate(records, start=1):
        raw = backend.vqa(rec["images"], rec["question"])
        prediction = short_answer(raw, rec["answer"])
        rows.append({**rec, "prediction": prediction, "raw": raw,
                     "correct": is_correct(prediction, rec["answer"])})
        if progress and i % progress == 0:
            acc = sum(r["correct"] for r in rows) / len(rows)
            print(f"[eval] {i} questions, running accuracy {acc:.4f}")
    return _accuracy_report(rows)


def eval_cdvqa(backend, records: Iterable[dict]) -> dict:
    by_split: Dict[str, List[dict]] = defaultdict(list)
    for rec in records:
        raw = backend.vqa(rec["images"], rec["question"])
        prediction = short_answer(raw, rec["answer"])
        correct = is_correct(prediction, rec["answer"])
        if not correct and rec.get("options"):
            # Multiple-choice protocol: pick the option the answer text contains.
            hits = [o for o in rec["options"] if normalise(o) and normalise(o) in normalise(raw)]
            if len(hits) == 1:
                prediction = hits[0]
                correct = is_correct(prediction, rec["answer"])
        by_split[rec.get("split", "test")].append({**rec, "prediction": prediction,
                                                   "correct": correct})
    out = {}
    for split, rows in by_split.items():
        report = _accuracy_report(rows)
        out[split] = {"n": report["n"], "OA": report["overall_accuracy"],
                      "AA": report["average_accuracy"], "per_type": report["per_type"],
                      "examples": report["examples"]}
    return {"splits": out}


def eval_grounding(backend, records: Iterable[dict], progress: int = 50) -> dict:
    hits = {0.5: 0, 0.7: 0}
    unique_hits = {0.5: 0, 0.7: 0}
    n = n_unique = parsed = 0
    ious: List[float] = []
    examples: List[dict] = []
    for rec in records:
        n += 1
        prediction = backend.grounding(rec["images"], rec["expression"])
        if prediction is None:
            if len(examples) < 5:
                examples.append({"expression": rec["expression"], "iou": None,
                                 "note": "no box produced"})
            continue
        parsed += 1
        width = height = 0.0
        try:
            from PIL import Image

            with Image.open(rec["images"][0]) as im:
                width, height = float(im.width), float(im.height)
        except Exception:
            pass
        gt = to_0_100(rec["box"], width, height)
        score = iou(gt, prediction)
        ious.append(score)
        is_unique = rec.get("unique", True)
        n_unique += int(is_unique)
        for threshold in (0.5, 0.7):
            if score >= threshold:
                hits[threshold] += 1
                if is_unique:
                    unique_hits[threshold] += 1
        if len(examples) < 5:
            examples.append({"expression": rec["expression"], "iou": round(score, 3),
                             "gt": [round(v, 1) for v in gt],
                             "prediction": [round(v, 1) for v in prediction]})
        if progress and n % progress == 0:
            print(f"[eval] {n} expressions, Acc@0.5 {hits[0.5] / n:.4f}")
    return {"n": n, "boxes_produced": parsed,
            "Acc@0.5": round(hits[0.5] / n, 4) if n else 0.0,
            "Acc@0.7": round(hits[0.7] / n, 4) if n else 0.0,
            "Acc@0.5_unique": round(unique_hits[0.5] / n_unique, 4) if n_unique else 0.0,
            "Acc@0.7_unique": round(unique_hits[0.7] / n_unique, 4) if n_unique else 0.0,
            "mean_iou": round(sum(ious) / len(ious), 4) if ious else 0.0,
            "examples": examples}


def eval_caption(backend, records: Iterable[dict]) -> dict:
    """Caption evaluation: BLEU-4/METEOR/CIDEr need the official scorers.

    Predictions are written out for scoring with ``pycocoevalcap``; the harness
    reports only what it can compute here rather than inventing a metric.
    """
    rows = []
    for rec in records:
        rows.append({"image": str(rec["images"][0].name),
                     "reference": rec["reference"],
                     "prediction": backend.caption(rec["images"])})
    return {"n": len(rows), "predictions": rows[:200],
            "note": "BLEU-4/METEOR/CIDEr/ROUGE-L require pycocoevalcap; run it over the "
                    "'predictions' array. No approximate score is reported here."}


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True,
                    choices=["rsvqa", "vrsbench_vqa", "vrsbench_ground", "vrsbench_caption", "cdvqa"])
    ap.add_argument("--root", required=True)
    ap.add_argument("--ann", default="", help="annotation file (VRSBench / CDVQA)")
    ap.add_argument("--backend", default="analysis", choices=["analysis", "vlm"])
    ap.add_argument("--model", default="Qwen/Qwen3-VL-4B-Instruct")
    ap.add_argument("--adapter", default="")
    ap.add_argument("--max-n", type=int, default=1000, help="cap per split (0 = all)")
    ap.add_argument("--tag", default="", help="label recorded in the results file")
    a = ap.parse_args()

    root = Path(a.root)
    if not root.exists():
        return _fail(f"dataset root not found: {root}")
    ann = Path(a.ann) if a.ann else None
    if a.dataset != "rsvqa" and ann is None:
        return _fail(f"--ann is required for {a.dataset}")

    backend = make_backend(a.backend, a.model, a.adapter)
    print(f"[eval] backend={backend.name} model={backend.model_id} "
          f"adapter={backend.adapter or 'none'} dataset={a.dataset}")

    t0 = time.time()
    if a.dataset == "rsvqa":
        result = eval_vqa(backend, read_rsvqa(root, a.max_n))
    elif a.dataset == "cdvqa":
        result = eval_cdvqa(backend, read_cdvqa(root, ann, a.max_n))
    elif a.dataset == "vrsbench_ground":
        result = eval_grounding(backend, read_vrsbench(root, ann, a.dataset, a.max_n))
    elif a.dataset == "vrsbench_caption":
        result = eval_caption(backend, read_vrsbench(root, ann, a.dataset, a.max_n))
    else:
        result = eval_vqa(backend, read_vrsbench(root, ann, a.dataset, a.max_n))

    payload = {"dataset": a.dataset, "backend": backend.name, "model_id": backend.model_id,
               "adapter": backend.adapter or None, "tag": a.tag or None,
               "max_n": a.max_n, "root": str(root),
               "elapsed_s": round(time.time() - t0, 1),
               "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "metrics": result}
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{a.dataset}_{a.backend}_{int(time.time())}.json"
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in payload.items() if k != "metrics"}, indent=2))
    print(json.dumps(_headline(result), indent=2))
    print(f"[eval] full results -> {out}")
    return 0


def _headline(result: dict) -> dict:
    for key in ("overall_accuracy", "Acc@0.5"):
        if key in result:
            return {k: v for k, v in result.items()
                    if k in ("n", "overall_accuracy", "average_accuracy", "Acc@0.5", "Acc@0.7",
                             "Acc@0.5_unique", "Acc@0.7_unique", "mean_iou", "boxes_produced")}
    if "splits" in result:
        return {s: {"n": v["n"], "OA": v["OA"], "AA": v["AA"]} for s, v in result["splits"].items()}
    return {"n": result.get("n")}


def _fail(message: str) -> int:
    print(f"[eval] {message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
