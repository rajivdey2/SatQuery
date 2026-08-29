"""Async job store + analyze endpoint for the SatQuery API."""
from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import HTTPException, UploadFile

from backend.config import UPLOAD_DIR, settings
from backend.controller.executor import run_pipeline

JOBS: Dict[str, dict] = {}


def create_job(files: List[UploadFile], query: str,
               model_params: Optional[dict] = None, ground_truth: Optional[str] = None,
               model_override: Optional[str] = None) -> str:
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    workdir = UPLOAD_DIR / job_id
    workdir.mkdir(parents=True, exist_ok=False)

    saved: List[str] = []
    for f in files:
        dest = workdir / f.filename.replace("\\", "_").replace("/", "_").replace(" ", "_")
        blob = f.file.read()
        if len(blob) > settings.max_file_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"{f.filename} exceeds {settings.max_file_mb} MB")
        dest.write_bytes(blob)
        saved.append(str(dest))

    JOBS[job_id] = {
        "id": job_id, "status": "queued", "query": query, "files": saved,
        "model_params": model_params or {},
        "ground_truth": ground_truth, "model_override": model_override,
        "created_at": __import__("time").time(),
        "result": None, "trace": None, "error": None,
    }
    asyncio.create_task(_run(job_id))
    return job_id


async def _run(job_id: str) -> None:
    job = JOBS[job_id]
    job["status"] = "running"
    try:
        trace, result = await asyncio.to_thread(
            run_pipeline, job["files"], job["query"], job_id,
            model_override=job.get("model_override"),
            model_params=job.get("model_params"),
            ground_truth=job.get("ground_truth"))
        job["trace"] = json.loads(trace.model_dump_json())
        if result is not None:
            job["result"] = result.to_dict()
            job["status"] = "done"
        else:
            job["result"] = {"rejected": record_rejection(trace)}
            job["status"] = "rejected"
    except Exception as exc:  # pragma: no cover
        job["status"] = "error"
        job["error"] = str(exc)


def record_rejection(trace) -> dict:
    return {"title": trace.outputs.text.text, "validation_failed": trace.validation.failed,
            "validation_warnings": trace.validation.warnings,
            "input_config": json.loads(trace.input_config.model_dump_json())}


def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}")
    return job