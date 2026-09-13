"""API surface: upload, poll, trace download, report download, registry."""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.api.main import app
from scripts.make_demo_data import (CRS_A, EXTENT_A, S2_BAND_NAMES, base_scene, evolve_scene,
                                    optical_stack, sar_stack, write_geotiff)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    root = tmp_path_factory.mktemp("api_scenes")
    labels = base_scene()
    later = evolve_scene(labels)[0]
    out = {}
    stack, _ = optical_stack(labels, seed=5)
    p = root / "areaA_20230101.tif"
    write_geotiff(p, stack, CRS_A, EXTENT_A, S2_BAND_NAMES,
                  {"ACQUISITION_DATE": "2023-01-01T04:30:00Z", "SENSOR": "Sentinel-2 MSI"})
    out["t1"] = p
    stack2, _ = optical_stack(later, seed=6)
    p2 = root / "areaA_20230701.tif"
    write_geotiff(p2, stack2, CRS_A, EXTENT_A, S2_BAND_NAMES,
                  {"ACQUISITION_DATE": "2023-07-01T04:31:00Z", "SENSOR": "Sentinel-2 MSI"})
    out["t2"] = p2
    p3 = root / "areaB_sar.tif"
    write_geotiff(p3, sar_stack(labels, seed=21), CRS_A, EXTENT_A, ["amplitude"],
                  {"ACQUISITION_DATE": "2023-03-15T05:12:00Z", "SENSOR": "RISAT-1"})
    out["sar"] = p3
    p4 = root / "areaB_optical.tif"
    optical_b, _ = optical_stack(labels, seed=17, cloud=True, bands=4)
    write_geotiff(p4, optical_b, CRS_A, EXTENT_A, ["blue", "green", "red", "nir"],
                  {"ACQUISITION_DATE": "2023-03-15T05:10:00Z", "SENSOR": "Cartosat-2S MX"})
    out["optical4"] = p4
    return out


def _analyze(client, paths, query, params=None, timeout_s: float = 120.0):
    """POST the images, then poll until the background controller run settles.

    The endpoint returns as soon as the job is queued -- the controller runs on a
    worker thread -- so a single GET would race it.
    """
    handles = [("files", (Path(p).name, Path(p).read_bytes(), "image/tiff")) for p in paths]
    data = {"query": query}
    if params is not None:
        data["model_params"] = json.dumps(params)
    response = client.post("/api/analyze", files=handles, data=data)
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]

    deadline = time.time() + timeout_s
    while True:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "rejected", "error"):
            break
        assert time.time() < deadline, f"job {job_id} stuck in {job['status']}"
        time.sleep(0.1)
    assert job["status"] != "error", job.get("error")
    return job


def test_health_reports_the_active_backend(client):
    payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["backend"] in ("analysis", "analysis+vlm", "mock")
    assert len(payload["tasks"]) == 6
    assert "adapted_head" in payload


def test_registry_endpoint_exposes_permitted_parameters(client):
    payload = client.get("/api/registry").json()
    ids = [e["id"] for e in payload["entries"]]
    assert "rs_vlm.grounding" in ids and "fusion.joint" in ids
    grounding = next(e for e in payload["entries"] if e["id"] == "rs_vlm.grounding")
    assert "target_class" in grounding["permitted_parameters"]
    assert grounding["permitted_parameters"]["target_class"]["choices"]
    assert grounding["tool_chain"]
    assert payload["specialists"]["single_grounding"]["required_images"] == 1


def test_demo_endpoint_lists_the_five_queries(client):
    payload = client.get("/api/demo").json()
    assert len(payload["queries"]) == 5
    assert payload["queries"][0]["expects"] == "single_caption"


def test_analyze_single_image_end_to_end(client, files):
    job = _analyze(client, [files["t1"]], "Describe the land-cover and major objects visible "
                                          "in this image.")
    assert job["status"] == "done"
    assert job["trace"]["task"] == "single_caption"
    assert job["result"]["text"]
    assert job["result"]["confidence"]["value"] is not None
    assert job["result"]["evidence"]


def test_analyze_pair_routes_to_change(client, files):
    job = _analyze(client, [files["t1"], files["t2"]],
                   "Has the built-up area increased, decreased, or remained unchanged?")
    assert job["trace"]["task"] == "change_vqa"
    assert job["result"]["mask_kind"] == "change_map"


def test_analyze_cross_modal_pair_routes_to_fusion(client, files):
    job = _analyze(client, [files["optical4"], files["sar"]],
                   "Use the optical and SAR images together to identify built-up and "
                   "water-covered regions.")
    assert job["trace"]["task"] == "sar_optical_fusion"
    assert job["trace"]["confidence"]["source"] == "cross_modal_agreement"


def test_evidence_images_are_served(client, files):
    job = _analyze(client, [files["t1"]], "Highlight the water body referred to in the query.")
    for item in job["trace"]["outputs"]["evidence"]:
        name = Path(item["path"]).name
        response = client.get(f"/api/files/{name}")
        assert response.status_code == 200, name
        assert response.headers["content-type"] == "image/png"


def test_trace_download_matches_the_job(client, files):
    job = _analyze(client, [files["t1"]], "Describe this image.")
    response = client.get(f"/api/jobs/{job['id']}/trace")
    assert response.status_code == 200
    payload = json.loads(response.content)
    assert payload["run_id"] == job["id"]
    assert payload["task"] == job["trace"]["task"]


def test_report_download_is_a_pdf(client, files):
    job = _analyze(client, [files["t1"], files["t2"]],
                   "What changed between these two dates, and where did the change occur?")
    response = client.get(f"/api/jobs/{job['id']}/report")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content[:4] == b"%PDF"
    assert len(response.content) > 20_000


def test_rejected_pair_returns_the_reasons(client, files, tmp_path):
    mismatch = tmp_path / "elsewhere.tif"
    write_geotiff(mismatch, optical_stack(base_scene(seed=71), seed=41)[0], "EPSG:32643",
                  (410000.0, 2000000.0, 413840.0, 2003840.0), S2_BAND_NAMES, {})
    job = _analyze(client, [files["t1"], mismatch], "What changed between these two dates?")
    assert job["status"] == "rejected"
    assert job["result"]["rejected"]["validation_failed"]
    response = client.get(f"/api/jobs/{job['id']}/report")
    assert response.status_code == 200          # a rejection is still reportable


def test_permitted_parameters_are_accepted_over_the_api(client, files):
    job = _analyze(client, [files["t1"]], "Highlight the water body.",
                   params={"target_class": "water", "max_regions": 3})
    assert job["trace"]["effective_parameters"]["target_class"] == "water"
    assert job["trace"]["registry_entries_used"][0]["rejected_parameters"] == []


def test_unpermitted_parameters_are_reported_not_applied(client, files):
    job = _analyze(client, [files["t1"]], "Describe this image.",
                   params={"internal_seed": 42})
    assert "internal_seed" in job["trace"]["registry_entries_used"][0]["rejected_parameters"]
    assert "internal_seed" not in job["trace"]["effective_parameters"]


def test_bad_model_params_are_a_client_error(client, files):
    handles = [("files", (files["t1"].name, files["t1"].read_bytes(), "image/tiff"))]
    response = client.post("/api/analyze", files=handles,
                           data={"query": "hello", "model_params": "not json"})
    assert response.status_code == 400


def test_unsupported_file_type_is_refused(client, tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("not an image", encoding="utf-8")
    response = client.post("/api/analyze",
                           files=[("files", (path.name, path.read_bytes(), "text/plain"))],
                           data={"query": "describe"})
    assert response.status_code == 415


def test_too_many_files_is_refused(client, files):
    handles = [("files", (files["t1"].name, files["t1"].read_bytes(), "image/tiff"))] * 3
    response = client.post("/api/analyze", files=handles, data={"query": "describe"})
    assert response.status_code == 400


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/job_does_not_exist").status_code == 404


def test_path_traversal_on_files_is_blocked(client):
    assert client.get("/api/files/..%2F..%2Fconfig.py").status_code in (404, 400)
