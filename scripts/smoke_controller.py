"""End-to-end smoke test of the graded controller core (mock backend).

Runs the five verbatim problem-statement demo queries, writes each audit trace
and a rendered PDF report, and prints a summary table. A rejection probe
verifies validator-gate behaviour on misaligned georeferenced pairs.
"""
from __future__ import annotations

import json
import mimetypes
import time
from pathlib import Path
from typing import List, Optional

from backend.config import TRACE_DIR, UPLOAD_DIR
from backend.controller.executor import run_pipeline
from backend.reports.pdf_report import build_report

DEMO = UPLOAD_DIR.parent / "demo_data"

CASES: List[dict] = [
    {"name": "single_caption", "files": ["optical_city.png"],
     "query": "Describe the land-cover and major objects visible in this image."},
    {"name": "single_grounding", "files": ["optical_city.png"],
     "query": "Highlight the water body referred to in the query."},
    {"name": "single_vqa", "files": ["optical_city.png"],
     "query": "What is the dominant land cover in this remote sensing image?"},
    {"name": "change_description", "files": ["areaA_20230101.tif", "areaA_20230701.tif"],
     "query": "Describe what changed between these two dates, and where the change occurred."},
    {"name": "change_vqa", "files": ["areaA_20230101.tif", "areaA_20230701.tif"],
     "query": "Has the built-up area increased, decreased, or remained unchanged?"},
    {"name": "sar_optical_fusion", "files": ["areaB_optical.tif", "areaB_sar.tif"],
     "query": "Use the optical and SAR images together to identify built-up and water-covered regions."},
    {"name": "REJECT_mismatched_crs", "files": ["areaA_20230101.tif", "areaB_sar.tif"],
     "query": "What changed between these two images?"},
]

SUMMARY = []


def main() -> None:
    for case in CASES:
        run_id = f"smoke_{int(time.time() * 1000)}"
        paths = [str(DEMO / f) for f in case["files"]]
        t0 = time.time()
        trace, result = run_pipeline(paths, case["query"], run_id)
        dt = time.time() - t0
        accepted = trace.validation.accepted
        if result is None:
            SUMMARY.append({"name": case["name"], "task": trace.task, "accepted": accepted,
                            "ms": int(dt * 1000), "text": trace.outputs.text.text})
            print(f"[REJECT] {case['name']}: {trace.outputs.text.text}")
            continue
        pdf = build_report(run_id, result.to_dict(), json.loads(trace.model_dump_json()))
        SUMMARY.append({"name": case["name"], "task": trace.task, "accepted": accepted,
                        "ms": int(dt * 1000), "text": result.text[:120],
                        "confidence": trace.confidence.source,
                        "trace": str(TRACE_DIR / f"{run_id}.json"), "pdf": str(pdf)})
        print(f"[ok] {case['name']:<18} task={trace.task:<20} conf={trace.confidence.source:<14} "
              f"{int(dt * 1000):>5}ms  boxes={len(result.boxes)}")

    out = DEMO.parent / "smoke_summary.json"
    out.write_text(json.dumps(SUMMARY, indent=2), encoding="utf-8")
    print(f"\nsummary -> {out}")


if __name__ == "__main__":
    main()