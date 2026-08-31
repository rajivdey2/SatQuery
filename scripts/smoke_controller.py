"""End-to-end smoke run over the five problem-statement queries plus the edge cases.

Runs the controller directly (no server) against the generated demo data, prints a
one-line-per-case summary, and writes ``backend/runtime/smoke_summary.json`` with
the routed task, tools used, confidence and timing for every case. That file is the
artefact to paste into a status update or a slide — it shows the routing decisions,
not just that nothing crashed.

Usage:
  python scripts/make_demo_data.py       # once, to create the inputs
  python scripts/smoke_controller.py
  python scripts/smoke_controller.py --verbose      # also print the answers
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import DEMO_DIR, RUNTIME_DIR  # noqa: E402
from backend.controller.executor import run_pipeline  # noqa: E402

# (label, files, query, expected task or None for a rejection)
CASES = [
    ("PS-1 scene description",
     ["areaA_20230101.tif"],
     "Describe the land-cover and major objects visible in this image.",
     "single_caption"),
    ("PS-2 grounding",
     ["areaA_20230101.tif"],
     "Highlight the water body referred to in the query.",
     "single_grounding"),
    ("PS-3 change description",
     ["areaA_20230101.tif", "areaA_20230701.tif"],
     "What changed between these two dates, and where did the change occur?",
     "change_description"),
    ("PS-4 optical+SAR fusion",
     ["areaB_optical.tif", "areaB_sar.tif"],
     "Use the optical and SAR images together to identify built-up and water-covered regions.",
     "sar_optical_fusion"),
    ("PS-5 change VQA",
     ["areaA_20230101.tif", "areaA_20230701.tif"],
     "Has the built-up area increased, decreased, or remained unchanged?",
     "change_vqa"),
    ("single-image VQA (presence)",
     ["areaA_20230101.tif"],
     "Is there any water body visible in this scene?",
     "single_vqa"),
    ("single-image VQA (extent)",
     ["areaA_20230101.tif"],
     "What percentage of the scene is built-up?",
     "single_vqa"),
    ("benchmark PNG (visible-only indices)",
     ["optical_city.png"],
     "Describe the land cover in this image.",
     "single_caption"),
    ("dual-pol SAR fusion",
     ["areaB_optical.tif", "areaB_sar_dualpol.tif"],
     "Use the optical and SAR images together to map water and built-up areas.",
     "sar_optical_fusion"),
    ("multi-tool sequence (fusion + grounding)",
     ["areaB_optical.tif", "areaB_sar.tif"],
     "Use both images together and highlight where the water is.",
     "sar_optical_fusion"),
    ("REJECT mismatched footprints",
     ["areaA_20230101.tif", "areaD_mismatch.tif"],
     "What changed between these two dates?",
     "rejected"),
    ("unlabelled SAR (statistical modality sniffing)",
     ["areaC_unlabelled.tif"],
     "Describe what this image shows.",
     None),
]


def _fmt_conf(confidence: Optional[dict]) -> str:
    if not confidence:
        return "—"
    value = confidence.get("value")
    source = confidence.get("source", "?")
    return f"{value:.2f} ({source})" if isinstance(value, (int, float)) else f"n/a ({source})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true", help="print the full answers")
    ap.add_argument("--only", default="", help="substring filter on the case label")
    a = ap.parse_args()

    demo = Path(DEMO_DIR)
    if not demo.exists() or not any(demo.glob("*.tif")):
        print("Demo inputs are missing. Run:  python scripts/make_demo_data.py")
        return 2

    truth_path = demo / "ground_truth.json"
    truth = json.loads(truth_path.read_text(encoding="utf-8")) if truth_path.exists() else {}

    rows: List[Dict] = []
    failures = 0
    print(f"{'case':40s} {'routed task':20s} {'tools':34s} {'conf':22s} {'ms':>6s}  ok")
    print("-" * 132)

    for label, names, query, expected in CASES:
        if a.only and a.only.lower() not in label.lower():
            continue
        files = [str(demo / n) for n in names]
        missing = [f for f in files if not Path(f).exists()]
        if missing:
            print(f"{label:40s} SKIPPED (missing {', '.join(Path(m).name for m in missing)})")
            continue
        run_id = f"smoke_{int(time.time() * 1000)}"
        started = time.perf_counter()
        try:
            trace, result = run_pipeline(files, query, run_id)
        except Exception as exc:
            failures += 1
            print(f"{label:40s} EXCEPTION {type(exc).__name__}: {exc}")
            rows.append({"case": label, "error": f"{type(exc).__name__}: {exc}"})
            continue
        elapsed = int((time.perf_counter() - started) * 1000)
        tools = " → ".join(e.id for e in trace.registry_entries_used) or "validator gate"
        ok = expected is None or trace.task == expected
        if not ok:
            failures += 1
        print(f"{label:40s} {trace.task:20s} {tools:34s} "
              f"{_fmt_conf(trace.confidence.model_dump()):22s} {elapsed:6d}  "
              f"{'✓' if ok else '✗ expected ' + str(expected)}")
        if a.verbose:
            print(f"    → {trace.outputs.text.text}\n")

        rows.append({
            "case": label, "query": query, "files": names,
            "expected_task": expected, "routed_task": trace.task,
            "matched": ok,
            "plan": list(trace.plan),
            "tools_used": [e.id for e in trace.registry_entries_used],
            "narration_source": trace.outputs.text.narration_source,
            "confidence": trace.confidence.model_dump(),
            "validation": {"accepted": trace.validation.accepted,
                           "warnings": len(trace.validation.warnings),
                           "failed": trace.validation.failed},
            "n_boxes": len(trace.outputs.boxes),
            "mask": trace.outputs.mask.kind,
            "evidence": [e.role for e in trace.outputs.evidence],
            "steps": [{"tool": s.tool, "ms": s.duration_ms, "status": s.status}
                      for s in trace.steps],
            "answer": trace.outputs.text.text,
            "execution_time_ms": trace.execution_time_ms,
            "trace": f"runtime/traces/{run_id}.json",
        })

    summary = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "cases": len(rows), "failures": failures,
               "ground_truth": truth.get("change_areaA"),
               "results": rows}
    out = Path(RUNTIME_DIR) / "smoke_summary.json"
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print("-" * 132)
    print(f"{len(rows)} cases, {failures} failure(s). Summary -> {out}")

    if truth.get("change_areaA"):
        change_rows = [r for r in rows if r.get("routed_task") in ("change_vqa", "change_description")]
        if change_rows:
            print(f"\nKnown change in areaA: built-up +{truth['change_areaA']['built_up_gain_ha']:.1f} ha, "
                  f"water -{truth['change_areaA']['water_loss_ha']:.1f} ha, "
                  f"{truth['change_areaA']['changed_fraction'] * 100:.1f}% of the scene.")
            print("Reported by the controller:")
            for r in change_rows:
                print(f"  {r['case']}: {r['answer'][:200]}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
