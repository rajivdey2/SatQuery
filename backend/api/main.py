"""SatQuery AI - FastAPI entrypoint.

Endpoints:
  GET  /health
  POST /api/analyze           multipart: files+, query, (model_override, model_params, ground_truth)
  GET  /api/jobs/{job_id}     job status + result + audit trace
  GET  /api/jobs/{job_id}/report   downloadable PDF (query, answer, evidence, audit trace)
  GET  /api/files/{name}      serve preview/overlay PNGs
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse

from backend.api.jobs import create_job, get_job
from backend.config import OUTPUT_DIR, settings
from backend.reports.pdf_report import build_report

app = FastAPI(title="SatQuery AI", version=settings.version)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": settings.version,
            "backend": "mock" if not settings.use_real_model else "model"}


@app.post("/api/analyze")
async def analyze(
    files: list[UploadFile] = File(...),
    query: str = Form(...),
    model_override: Optional[str] = Form(None),
    ground_truth: Optional[str] = Form(None),
    model_params: Optional[str] = Form(None),
) -> dict:
    params = None
    if model_params:
        try:
            params = json.loads(model_params)
            if not isinstance(params, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="model_params must be a JSON object")
    job_id = create_job(files, query, model_params=params,
                        ground_truth=ground_truth, model_override=model_override)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def get_run(job_id: str) -> dict:
    return get_job(job_id)


@app.get("/api/jobs/{job_id}/report")
def download_report(job_id: str):
    job = get_job(job_id)
    if job["status"] not in ("done", "rejected"):
        raise HTTPException(status_code=409, detail=f"Job {job_id} not finished: {job['status']}")
    result = job.get("result") or {}
    trace = job.get("trace") or {}
    try:
        pdf = build_report(job_id, result, trace)
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}")
    return FileResponse(pdf, media_type="application/pdf",
                        filename=f"satquery_{job_id}.pdf")


@app.get("/api/files/{name}")
def serve_file(name: str):
    path = (OUTPUT_DIR / name).resolve()
    if not str(path).startswith(str(OUTPUT_DIR.resolve())) or not path.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path, media_type="image/png")