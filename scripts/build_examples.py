"""Bake the showcase examples the dashboard shows judges on load.

Runs each curated case once through the real controller and stores the complete
job record -- answer, measurements, rendered evidence, audit trace -- under
``backend/runtime/examples``. The dashboard then renders them instantly through the
same components a live run uses, so a judge sees finished work the moment the page
opens and can still press "run live" to watch it computed.

The examples are the five representative queries from the problem statement,
verbatim, plus three cases that show behaviour a polished demo usually hides: an
input the controller refuses, a query that needs two registry entries, and a
benchmark PNG whose missing near-infrared band forces a degraded, clearly-labelled
answer.

Usage
  python scripts/make_demo_data.py       # once, to create the inputs
  python scripts/build_examples.py
  python scripts/build_examples.py --mock     # bake with SATQUERY_MOCK phrasing
  python scripts/build_examples.py --only ps1
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# (slug, title, subtitle, files, query, expected task, why it is worth showing)
CASES = [
    ("ps1-scene-description",
     "Describe the land cover and major objects",
     "Problem-statement query 1 · single-image scene description",
     ["areaA_20230101.tif"],
     "Describe the land-cover and major objects visible in this image.",
     "single_caption",
     "Twelve-band Sentinel-2-style input: every spectral index is available, so class "
     "extents come with hectares and a high separability score."),
    ("ps2-grounding",
     "Highlight the water body",
     "Problem-statement query 2 · text-guided grounding",
     ["areaA_20230101.tif"],
     "Highlight the water body referred to in the query.",
     "single_grounding",
     "The referring expression is resolved to a class, then to a connected region, and "
     "returned as a box in the same 0–100 coordinates the audit trace reports."),
    ("ps3-change-description",
     "What changed, and where",
     "Problem-statement query 3 · bi-temporal change description",
     ["areaA_20230101.tif", "areaA_20230701.tif"],
     "What changed between these two dates, and where did the change occur?",
     "change_description",
     "Change vector analysis against an estimated noise floor, a per-class transition "
     "table, and a rendered before / after / change triptych."),
    ("ps4-optical-sar-fusion",
     "Use optical and SAR together",
     "Problem-statement query 4 · cross-modal joint extraction",
     ["areaB_optical.tif", "areaB_sar.tif"],
     "Use the optical and SAR images together to identify built-up and water-covered regions.",
     "sar_optical_fusion",
     "Both sensors are measured independently and then compared: per-class Cohen's kappa, "
     "and the area the radar recovers where cloud blocks the optical scene."),
    ("ps5-change-vqa",
     "Has the built-up area increased?",
     "Problem-statement query 5 · change-based VQA",
     ["areaA_20230101.tif", "areaA_20230701.tif"],
     "Has the built-up area increased, decreased, or remained unchanged?",
     "change_vqa",
     "A direction verdict that only fires when the measured delta clears both the noise "
     "floor and the classifier's own flip rate on unchanged ground."),
    ("edge-rejected-pair",
     "A pair the controller refuses",
     "Compatibility gate · rejection with an explanation",
     ["areaA_20230101.tif", "areaD_mismatch.tif"],
     "What changed between these two dates?",
     "rejected",
     "Two scenes that do not overlap. The controller measures the overlap, refuses the "
     "run, and says why — the behaviour the unseen ISRO evaluation set rewards."),
    ("edge-two-tool-plan",
     "Fusion plus localisation",
     "Agentic orchestration · two registry entries in one plan",
     ["areaB_optical.tif", "areaB_sar.tif"],
     "Use the optical and SAR images together and highlight where the water is.",
     "sar_optical_fusion",
     "The query asks for something the fusion entry does not produce, so the router adds "
     "the grounding entry and the trace shows both steps."),
    ("edge-benchmark-png",
     "A benchmark PNG with no near-infrared",
     "Graceful degradation · visible-band proxies only",
     ["optical_city.png"],
     "Describe the land cover in this image.",
     "single_caption",
     "NDVI and NDWI are reported unavailable rather than silently approximated; the answer "
     "carries the caveat and the confidence is penalised."),
]


def _slug_ok(slug: str, only: str) -> bool:
    return not only or only.lower() in slug.lower()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="substring filter on the example slug")
    ap.add_argument("--mock", action="store_true",
                    help="bake with SATQUERY_MOCK=1 (template phrasing over real measurements)")
    ap.add_argument("--clean", action="store_true", help="delete previously baked examples first")
    a = ap.parse_args()

    if a.mock:
        os.environ["SATQUERY_MOCK"] = "1"

    from backend.config import DEMO_DIR, EXAMPLE_DIR, settings  # noqa: E402
    from backend.controller.executor import run_pipeline  # noqa: E402

    demo = Path(DEMO_DIR)
    if not any(demo.glob("*.tif")):
        print("Demo inputs are missing. Run:  python scripts/make_demo_data.py")
        return 2

    out_dir = Path(EXAMPLE_DIR)
    if a.clean and out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    index: List[Dict] = []
    failures = 0
    print(f"baking examples with backend="
          f"{'mock' if settings.use_mock else 'analysis engine'}\n")
    print(f"{'example':26s} {'routed task':20s} {'conf':20s} {'ms':>6s}  ok")
    print("-" * 92)

    for slug, title, subtitle, names, query, expected, highlight in CASES:
        if not _slug_ok(slug, a.only):
            continue
        files = [str(demo / n) for n in names]
        missing = [Path(f).name for f in files if not Path(f).exists()]
        if missing:
            print(f"{slug:26s} SKIPPED (missing {', '.join(missing)})")
            continue

        run_id = f"example_{slug.replace('-', '_')}"
        started = time.perf_counter()
        try:
            trace, result = run_pipeline(files, query, run_id)
        except Exception as exc:
            failures += 1
            print(f"{slug:26s} EXCEPTION {type(exc).__name__}: {exc}")
            continue
        elapsed = int((time.perf_counter() - started) * 1000)

        job = json.loads(trace.model_dump_json())
        record = {
            "id": run_id,
            "status": "rejected" if trace.task == "rejected" else "done",
            "query": query,
            "filenames": list(names),
            "files": files,
            "created_at": time.time(),
            "result": result.to_dict() if result is not None else None,
            "trace": job,
            "error": None,
            "example": {
                "slug": slug, "title": title, "subtitle": subtitle,
                "query": query, "inputs": list(names), "highlight": highlight,
                "expects": expected,
                "baked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "backend": "mock" if settings.use_mock else "analysis_engine",
            },
        }
        (out_dir / f"{slug}.json").write_text(json.dumps(record, indent=2, default=str),
                                             encoding="utf-8")

        confidence = trace.confidence.model_dump()
        value = confidence.get("value")
        conf_text = (f"{value:.2f} ({confidence['source']})" if isinstance(value, (int, float))
                     else f"n/a ({confidence['source']})")
        ok = trace.task == expected
        failures += 0 if ok else 1
        print(f"{slug:26s} {trace.task:20s} {conf_text:20s} {elapsed:6d}  "
              f"{'OK' if ok else 'MISMATCH expected ' + expected}")

        evidence = ((job.get("outputs") or {}).get("evidence") or [])
        index.append({
            "slug": slug, "title": title, "subtitle": subtitle, "query": query,
            "inputs": list(names), "highlight": highlight,
            "expects": expected, "routed_task": trace.task, "matched": ok,
            "status": record["status"],
            "tools_used": [e.id for e in trace.registry_entries_used],
            "plan": list(trace.plan),
            "confidence": {"value": value, "source": confidence.get("source")},
            "execution_time_ms": trace.execution_time_ms,
            "n_boxes": len(trace.outputs.boxes),
            "mask_kind": trace.outputs.mask.kind,
            "thumbnail": (evidence[-1]["path"] if evidence else None),
            "answer_preview": _preview(trace.outputs.text.text),
        })

    payload = {
        "available": bool(index),
        "baked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "backend": "mock" if settings.use_mock else "analysis_engine",
        "server_version": settings.version,
        "examples": index,
        "note": ("Pre-computed with the real controller: the answers, measurements and audit "
                 "traces below are the actual output for these inputs, stored so the dashboard "
                 "is not empty on load. Each one can be re-run live."),
    }
    (out_dir / "index.json").write_text(json.dumps(payload, indent=2, default=str),
                                        encoding="utf-8")
    print("-" * 92)
    print(f"{len(index)} example(s) baked, {failures} mismatch/failure(s) -> {out_dir}")
    print("The dashboard now shows these on load; /api/examples serves the index.")
    return 1 if failures else 0


def _preview(text: str, limit: int = 190) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


if __name__ == "__main__":
    raise SystemExit(main())
