"""API smoke test: exercises the HTTP surface in-process, no server required.

Runs the five problem-statement queries plus the rejection and parameter cases
through FastAPI's test client, checks the response shapes, downloads a PDF report
and the audit trace, and writes ``backend/runtime/api_results.json``.

Usage:
  python scripts/make_demo_data.py     # once
  python scripts/test_api.py
  python scripts/test_api.py --base-url http://localhost:8000    # hit a live server
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import DEMO_DIR, RUNTIME_DIR  # noqa: E402

CASES = [
    (["areaA_20230101.tif"], "Describe the land-cover and major objects visible in this image.",
     "single_caption", None),
    (["areaA_20230101.tif"], "Highlight the water body referred to in the query.",
     "single_grounding", None),
    (["areaA_20230101.tif", "areaA_20230701.tif"],
     "What changed between these two dates, and where did the change occur?",
     "change_description", None),
    (["areaB_optical.tif", "areaB_sar.tif"],
     "Use the optical and SAR images together to identify built-up and water-covered regions.",
     "sar_optical_fusion", None),
    (["areaA_20230101.tif", "areaA_20230701.tif"],
     "Has the built-up area increased, decreased, or remained unchanged?",
     "change_vqa", None),
    (["areaA_20230101.tif"], "Highlight the built-up area.", "single_grounding",
     {"target_class": "built_up", "max_regions": 2, "not_permitted": 1}),
    (["areaA_20230101.tif", "areaD_mismatch.tif"], "What changed between these two dates?",
     "rejected", None),
]


class LiveClient:
    """Minimal adapter so the same code can hit a running server."""

    def __init__(self, base_url: str):
        import httpx

        self._client = httpx.Client(base_url=base_url, timeout=300.0)

    def get(self, url, **kw):
        return self._client.get(url, **kw)

    def post(self, url, **kw):
        return self._client.post(url, **kw)


def make_client(base_url: str):
    if base_url:
        return LiveClient(base_url)
    from fastapi.testclient import TestClient

    from backend.api.main import app

    return TestClient(app)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="", help="test a live server instead of the in-process app")
    a = ap.parse_args()

    demo = Path(DEMO_DIR)
    if not any(demo.glob("*.tif")):
        print("Demo inputs are missing. Run:  python scripts/make_demo_data.py")
        return 2

    client = make_client(a.base_url)
    health = client.get("/health").json()
    print(f"health: backend={health.get('backend')} version={health.get('version')} "
          f"device={health.get('device')} adapted_head={health.get('adapted_head')}")
    registry = client.get("/api/registry").json()
    print(f"registry: {len(registry['entries'])} entries -> "
          f"{', '.join(e['id'] for e in registry['entries'])}")

    rows: List[dict] = []
    failures = 0
    for names, query, expected, params in CASES:
        files = []
        for name in names:
            path = demo / name
            if not path.exists():
                print(f"SKIP {name} (missing)")
                files = []
                break
            files.append(("files", (name, path.read_bytes(), "image/tiff")))
        if not files:
            continue
        data = {"query": query}
        if params:
            data["model_params"] = json.dumps(params)
        response = client.post("/api/analyze", files=files, data=data)
        if response.status_code != 200:
            failures += 1
            print(f"✗ POST /api/analyze -> {response.status_code}: {response.text[:200]}")
            continue
        job_id = response.json()["job_id"]
        job = _wait(client, job_id)
        trace = job.get("trace") or {}
        result = job.get("result") or {}
        ok = trace.get("task") == expected
        failures += 0 if ok else 1

        report = client.get(f"/api/jobs/{job_id}/report")
        trace_dl = client.get(f"/api/jobs/{job_id}/trace")
        pdf_ok = report.status_code == 200 and report.content[:4] == b"%PDF"
        evidence_ok = True
        for item in trace.get("outputs", {}).get("evidence", []):
            name = Path(item["path"]).name
            evidence_ok &= client.get(f"/api/files/{name}").status_code == 200

        confidence = (result.get("confidence") or {})
        print(f"{'✓' if ok and pdf_ok and evidence_ok else '✗'} {trace.get('task', job['status']):20s} "
              f"conf={confidence.get('value')} ({confidence.get('source')}) "
              f"pdf={len(report.content) // 1024}KB "
              f"trace={trace_dl.status_code} evidence={'ok' if evidence_ok else 'MISSING'} "
              f"| {query[:56]}")
        rows.append({
            "query": query, "files": names, "expected": expected,
            "routed_task": trace.get("task"), "status": job["status"], "matched": ok,
            "tools_used": result.get("tools_used"),
            "rejected_parameters": [p for e in trace.get("registry_entries_used", [])
                                    for p in e.get("rejected_parameters", [])],
            "effective_parameters": trace.get("effective_parameters"),
            "confidence": confidence, "n_boxes": len(result.get("boxes") or []),
            "mask_kind": result.get("mask_kind"),
            "execution_time_ms": trace.get("execution_time_ms"),
            "pdf_bytes": len(report.content), "pdf_ok": pdf_ok,
            "trace_download_ok": trace_dl.status_code == 200,
            "evidence_served": evidence_ok,
            "answer": result.get("text"),
        })
        if not pdf_ok or not evidence_ok:
            failures += 1

    bad_request = client.post("/api/analyze",
                              files=[("files", ("x.tif", b"not a raster", "image/tiff"))],
                              data={"query": "describe"})
    print(f"{'✓' if bad_request.status_code == 200 else '✗'} corrupt raster accepted for "
          f"validation ({bad_request.status_code})")

    out = Path(RUNTIME_DIR) / "api_results.json"
    out.write_text(json.dumps({"health": health, "cases": rows, "failures": failures},
                              indent=2, default=str), encoding="utf-8")
    print(f"\n{len(rows)} cases, {failures} failure(s). Details -> {out}")
    return 1 if failures else 0


def _wait(client, job_id: str, tries: int = 600) -> dict:
    import time

    for _ in range(tries):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "rejected", "error"):
            return job
        time.sleep(0.25)
    return {"status": "timeout", "id": job_id}


if __name__ == "__main__":
    raise SystemExit(main())
