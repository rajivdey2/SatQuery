"""SatQuery AI — FastAPI entrypoint.

Endpoints
  GET  /health                      backend + adapted-head status
  GET  /api/registry                the predefined model/tool registry (graded artefact)
  GET  /api/demo                    bundled demo inputs and the five problem-statement queries
  POST /api/analyze                 multipart: files+, query, [model_params, model_override]
  GET  /api/jobs                    recent jobs
  GET  /api/jobs/{id}               status + result + audit trace
  GET  /api/jobs/{id}/trace         the audit trace as a downloadable JSON file
  GET  /api/jobs/{id}/report        downloadable PDF report
  GET  /api/files/{name}            rendered evidence PNGs
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, JSONResponse

from backend.analysis import scene_labels
from backend.api.jobs import create_job, get_job, list_jobs
from backend.config import DEMO_DIR, OUTPUT_DIR, TRACE_DIR, settings
from backend.controller.registry import describe_registry
from backend.reports.pdf_report import build_report
from backend.specialists import describe_all
from backend.specialists.backends import model_loader

app = FastAPI(title="SatQuery AI",
              version=settings.version,
              description="Agentic vision-language assistant for multimodal remote-sensing "
                          "image analysis (SIH problem statement 26167, ISRO/SAC).")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The five representative queries from the problem statement, verbatim.
DEMO_QUERIES = [
    {"query": "Describe the land-cover and major objects visible in this image.",
     "expects": "single_caption", "inputs": ["areaA_20230101.tif"]},
    {"query": "Highlight the water body referred to in the query.",
     "expects": "single_grounding", "inputs": ["areaA_20230101.tif"]},
    {"query": "What changed between these two dates, and where did the change occur?",
     "expects": "change_description", "inputs": ["areaA_20230101.tif", "areaA_20230701.tif"]},
    {"query": "Use the optical and SAR images together to identify built-up and water-covered regions.",
     "expects": "sar_optical_fusion", "inputs": ["areaB_optical.tif", "areaB_sar.tif"]},
    {"query": "Has the built-up area increased, decreased, or remained unchanged?",
     "expects": "change_vqa", "inputs": ["areaA_20230101.tif", "areaA_20230701.tif"]},
]


@app.get("/health")
def health() -> dict:
    head = scene_labels.describe()
    return {"status": "ok", "version": settings.version,
            "backend": ("mock" if settings.use_mock else
                        "analysis+vlm" if model_loader.available() else "analysis"),
            "device": settings.device,
            "adapted_head": {"available": head.get("available", False),
                             "model_id": head.get("model_id")},
            "narration": model_loader.describe(),
            "tasks": sorted(describe_all())}


@app.get("/api/registry")
def registry() -> dict:
    """The predefined registry the controller selects from, plus the specialists."""
    payload = describe_registry()
    payload["specialists"] = describe_all()
    return payload


@app.get("/api/demo")
def demo() -> dict:
    """Bundled demo inputs, so the GUI can run the five queries with one click."""
    demo_dir = Path(DEMO_DIR)
    files = sorted(p.name for p in demo_dir.glob("*") if p.suffix.lower() in
                   {".tif", ".tiff", ".png", ".jpg", ".jpeg"}) if demo_dir.exists() else []
    return {"available": bool(files), "directory": str(demo_dir), "files": files,
            "queries": DEMO_QUERIES,
            "hint": ("Run `python scripts/make_demo_data.py` to (re)generate these inputs."
                     if not files else "")}


@app.get("/api/demo/files/{name}")
def demo_file(name: str):
    path = (Path(DEMO_DIR) / Path(name).name).resolve()
    if not str(path).startswith(str(Path(DEMO_DIR).resolve())) or not path.exists():
        raise HTTPException(status_code=404, detail="demo file not found")
    return FileResponse(path, filename=path.name)


@app.post("/api/analyze")
async def analyze(
    files: List[UploadFile] = File(...),
    query: str = Form(...),
    model_override: Optional[str] = Form(None),
    ground_truth: Optional[str] = Form(None),
    model_params: Optional[str] = Form(None),
) -> dict:
    params = None
    if model_params:
        try:
            params = json.loads(model_params)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400,
                                detail=f"model_params must be a JSON object: {exc}") from exc
        if not isinstance(params, dict):
            raise HTTPException(status_code=400, detail="model_params must be a JSON object")
    job_id = create_job(files, query, model_params=params,
                        ground_truth=ground_truth, model_override=model_override)
    return {"job_id": job_id}


@app.get("/api/jobs")
def jobs(limit: int = 25) -> dict:
    return {"jobs": list_jobs(limit=limit)}


@app.get("/api/jobs/{job_id}")
def get_run(job_id: str) -> dict:
    return get_job(job_id)


@app.get("/api/jobs/{job_id}/trace")
def download_trace(job_id: str):
    get_job(job_id)                        # 404s for unknown ids
    path = Path(TRACE_DIR) / f"{job_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="No audit trace written for this job yet.")
    return FileResponse(path, media_type="application/json",
                        filename=f"satquery_trace_{job_id}.json")


@app.get("/api/jobs/{job_id}/report")
def download_report(job_id: str):
    job = get_job(job_id)
    if job["status"] not in ("done", "rejected"):
        raise HTTPException(status_code=409,
                            detail=f"Job {job_id} is {job['status']}; the report is not ready.")
    try:
        pdf = build_report(job_id, job.get("result") or {}, job.get("trace") or {})
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}") from exc
    return FileResponse(pdf, media_type="application/pdf", filename=f"satquery_{job_id}.pdf")


@app.get("/api/files/{name}")
def serve_file(name: str):
    root = Path(OUTPUT_DIR).resolve()
    path = (root / Path(name).name).resolve()
    if not str(path).startswith(str(root)) or not path.exists():
        raise HTTPException(status_code=404, detail="file not found")
    suffix = path.suffix.lower()
    media = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
             "pdf": "application/pdf", "json": "application/json"}.get(suffix.lstrip("."),
                                                                      "application/octet-stream")
    return FileResponse(path, media_type=media)


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):  # pragma: no cover
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail, "path": str(request.url.path)})
