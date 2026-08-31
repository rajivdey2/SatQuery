"""Job store and pipeline runner for the SatQuery API.

Jobs run on a worker thread so a large GeoTIFF cannot block the event loop, and
each job's record is mirrored to disk under ``runtime/jobs`` so a trace, an answer
and a report survive a server restart -- which matters when a judge reopens a link
minutes after the demo.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, UploadFile

from backend.config import JOB_DIR, UPLOAD_DIR, settings
from backend.controller.executor import run_pipeline

JOBS: Dict[str, dict] = {}
_SAFE = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}
_MAX_KEEP = 200


def _safe_name(name: str) -> str:
    stem = Path(name or "upload").name.replace("\\", "_").replace("/", "_").replace(" ", "_")
    return stem or "upload"


def _persist(job: dict) -> None:
    try:
        path = Path(JOB_DIR) / f"{job['id']}.json"
        path.write_text(json.dumps(job, indent=2, default=str), encoding="utf-8")
    except OSError:  # pragma: no cover - disk issues must not kill a request
        pass


def _prune() -> None:
    if len(JOBS) <= _MAX_KEEP:
        return
    for job_id in sorted(JOBS, key=lambda k: JOBS[k].get("created_at", 0))[: len(JOBS) - _MAX_KEEP]:
        JOBS.pop(job_id, None)


def create_job(files: List[UploadFile], query: str,
               model_params: Optional[dict] = None, ground_truth: Optional[str] = None,
               model_override: Optional[str] = None) -> str:
    """Save the uploads, register the job, and schedule the controller run."""
    if not files:
        raise HTTPException(status_code=400, detail="At least one image file is required.")
    if len(files) > settings.max_images:
        raise HTTPException(
            status_code=400,
            detail=(f"{len(files)} files supplied; the defined input scope is one image or one pair "
                    f"(max {settings.max_images})."))
    if not (query or "").strip():
        raise HTTPException(status_code=400, detail="A natural-language query is required.")

    job_id = f"job_{uuid.uuid4().hex[:12]}"
    workdir = Path(UPLOAD_DIR) / job_id
    workdir.mkdir(parents=True, exist_ok=True)

    saved: List[str] = []
    limit = settings.max_file_mb * 1024 * 1024
    for f in files:
        name = _safe_name(f.filename)
        if Path(name).suffix.lower() not in _SAFE:
            shutil.rmtree(workdir, ignore_errors=True)
            raise HTTPException(
                status_code=415,
                detail=(f"{name}: unsupported file type. GeoTIFF/TIFF for geospatial imagery, "
                        "PNG/JPEG only for benchmark datasets."))
        dest = workdir / name
        written = 0
        with dest.open("wb") as out:
            while True:
                chunk = f.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    out.close()
                    shutil.rmtree(workdir, ignore_errors=True)
                    raise HTTPException(status_code=413,
                                        detail=f"{name} exceeds the {settings.max_file_mb} MB limit.")
                out.write(chunk)
        saved.append(str(dest))

    JOBS[job_id] = {
        "id": job_id, "status": "queued", "query": query, "files": saved,
        "filenames": [Path(p).name for p in saved],
        "model_params": model_params or {}, "ground_truth": ground_truth,
        "model_override": model_override, "created_at": time.time(),
        "result": None, "trace": None, "error": None,
    }
    _persist(JOBS[job_id])
    _prune()

    try:
        asyncio.get_running_loop().create_task(_run(job_id))
    except RuntimeError:                       # no loop: run synchronously (scripts, tests)
        _run_sync(job_id)
    return job_id


def _finish(job_id: str, trace, result, error: Optional[str] = None) -> None:
    job = JOBS[job_id]
    if trace is not None:
        job["trace"] = json.loads(trace.model_dump_json())
    if result is not None:
        job["result"] = result.to_dict()
        job["status"] = "rejected" if result.task == "rejected" else "done"
    elif error:
        job["status"] = "error"
        job["error"] = error
    else:
        job["status"] = "rejected"
        job["result"] = {"rejected": {"title": "Input rejected",
                                      "validation_failed": list(trace.validation.failed)
                                      if trace else []}}
    job["finished_at"] = time.time()
    _persist(job)


def _invoke(job_id: str):
    job = JOBS[job_id]
    return run_pipeline(job["files"], job["query"], job_id,
                        model_override=job.get("model_override"),
                        model_params=job.get("model_params"),
                        ground_truth=job.get("ground_truth"))


async def _run(job_id: str) -> None:
    JOBS[job_id]["status"] = "running"
    try:
        trace, result = await asyncio.to_thread(_invoke, job_id)
        _finish(job_id, trace, result)
    except Exception as exc:  # pragma: no cover - unexpected controller failure
        _finish(job_id, None, None, error=f"{type(exc).__name__}: {exc}")


def _run_sync(job_id: str) -> None:
    JOBS[job_id]["status"] = "running"
    try:
        trace, result = _invoke(job_id)
        _finish(job_id, trace, result)
    except Exception as exc:
        _finish(job_id, None, None, error=f"{type(exc).__name__}: {exc}")


def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if job is None:
        path = Path(JOB_DIR) / f"{job_id}.json"
        if path.exists():
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                JOBS[job_id] = job
            except json.JSONDecodeError:
                job = None
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}")
    return job


def list_jobs(limit: int = 25) -> List[dict]:
    ordered = sorted(JOBS.values(), key=lambda j: -(j.get("created_at") or 0))[:limit]
    return [{"id": j["id"], "status": j["status"], "query": j["query"],
             "task": (j.get("trace") or {}).get("task"),
             "files": j.get("filenames", []),
             "created_at": j.get("created_at"),
             "confidence": ((j.get("result") or {}).get("confidence") or {}).get("value")}
            for j in ordered]
