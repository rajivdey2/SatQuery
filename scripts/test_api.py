"""Hit the running API with the demo data: analyze -> poll -> download report."""
from __future__ import annotations

import http.client
import json
import time
import uuid
import urllib.request
from pathlib import Path

BASE = "127.0.0.1", 8000


def request(method: str, path: str, body=None, headers=None):
    c = http.client.HTTPConnection(*BASE, timeout=60)
    c.request(method, path, body, headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def upload(files, query, extra_form=None):
    boundary = uuid.uuid4().hex
    body = b""
    for fp in files:
        fn = Path(fp).name
        data = Path(fp).read_bytes()
        body += (f"--{boundary}\r\n"
                 f"Content-Disposition: form-data; name=\"files\"; filename=\"{fn}\"\r\n"
                 f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + b"\r\n"
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"query\"\r\n\r\n{query}\r\n".encode()
    for k, v in (extra_form or {}).items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    body += f"--{boundary}--\r\n".encode()
    status, data = request("POST", "/api/analyze", body,
                           {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    assert status == 200, (status, data[:200])
    return json.loads(data)["job_id"]


def poll(job_id):
    for _ in range(60):
        status, data = request("GET", f"/api/jobs/{job_id}")
        job = json.loads(data)
        if job["status"] in ("done", "rejected", "error"):
            return job
        time.sleep(0.5)
    raise TimeoutError(job_id)


DEMO = Path(r"C:\project\satquery\backend\runtime\demo_data")

CASES = [
    (["optical_city.png"], "Describe the land-cover and major objects visible in this image."),
    (["optical_city.png"], "Highlight the water body referred to in the query."),
    (["areaA_20230101.tif", "areaA_20230701.tif"],
     "What changed between these two dates, and where did the change occur?"),
    (["areaB_optical.tif", "areaB_sar.tif"],
     "Use the optical and SAR images together to identify built-up and water-covered regions."),
]

if __name__ == "__main__":
    results = []
    for files, query in CASES:
        job_id = upload([str(DEMO / f) for f in files], query)
        job = poll(job_id)
        res = job.get("result") or {}
        trace = job.get("trace") or {}
        results.append({"query": query[:60], "status": job["status"],
                        "task": res.get("task", trace.get("task")),
                        "text": (res.get("text") or res.get("rejected", {}).get("title", ""))[:80],
                        "boxes": len(res.get("boxes") or []),
                        "confidence": (res.get("confidence") or {}).get("source")})
        print(f"[{job['status']:>8}] {trace.get('task','?'):<20} job={job_id}")
        # download the PDF to prove the report endpoint
        status, pdf = request("GET", f"/api/jobs/{job_id}/report")
        if status == 200:
            out = Path(r"C:\project\satquery\backend\runtime\outputs") / f"{job_id}.pdf"
            out.write_bytes(pdf)
            print(f"        pdf -> {out.name} ({len(pdf)} bytes)")
    Path(r"C:\project\satquery\backend\runtime\api_results.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))