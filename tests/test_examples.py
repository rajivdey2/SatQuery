"""Showcase examples: the store, the endpoints, and the bake script's contract.

The examples exist so a judge sees finished work on load. They must therefore be
(a) served in exactly the job shape the GUI already renders, (b) honest about
missing evidence rather than silently showing a broken image, and (c) absent
gracefully when nobody has baked them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api import examples as store
from backend.api.main import app


def _record(slug: str = "ps1-scene-description", evidence_name: str = "ex_preview.png") -> dict:
    return {
        "id": f"example_{slug.replace('-', '_')}",
        "status": "done",
        "query": "Describe the land-cover and major objects visible in this image.",
        "filenames": ["areaA_20230101.tif"],
        "files": ["/demo/areaA_20230101.tif"],
        "result": {"task": "single_caption", "text": "Vegetation covers 52.4% (7731.0 ha).",
                   "boxes": [], "confidence": {"value": 0.71, "source": "measurement_composite"},
                   "tools_used": ["rs_vlm.caption"]},
        "trace": {"run_id": f"example_{slug}", "task": "single_caption",
                  "schema_version": "2.0.0",
                  "outputs": {"evidence": [{"role": "class_map", "path": evidence_name,
                                            "caption": "Measured land-cover classes"}]}},
        "error": None,
        "example": {"slug": slug, "title": "Describe the land cover",
                    "subtitle": "Problem-statement query 1", "expects": "single_caption",
                    "inputs": ["areaA_20230101.tif"], "highlight": "why this matters",
                    "backend": "analysis_engine"},
    }


def _index(slug: str = "ps1-scene-description") -> dict:
    return {"available": True, "baked_at": "2026-08-31T12:00:00", "backend": "analysis_engine",
            "note": "Pre-computed with the real controller.",
            "examples": [{"slug": slug, "title": "Describe the land cover",
                          "subtitle": "Problem-statement query 1", "expects": "single_caption",
                          "routed_task": "single_caption", "matched": True, "status": "done",
                          "inputs": ["areaA_20230101.tif"], "highlight": "why this matters",
                          "tools_used": ["rs_vlm.caption"], "plan": ["rs_vlm.caption"],
                          "confidence": {"value": 0.71, "source": "measurement_composite"},
                          "execution_time_ms": 412, "n_boxes": 0, "mask_kind": "class_map",
                          "thumbnail": "ex_preview.png",
                          "answer_preview": "Vegetation covers 52.4% (7731.0 ha)."}]}


@pytest.fixture
def baked(tmp_path, monkeypatch):
    """A baked example on disk, with its rendered evidence present."""
    example_dir = tmp_path / "examples"
    output_dir = tmp_path / "outputs"
    example_dir.mkdir()
    output_dir.mkdir()
    (output_dir / "ex_preview.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    (example_dir / "ps1-scene-description.json").write_text(json.dumps(_record()), encoding="utf-8")
    (example_dir / "index.json").write_text(json.dumps(_index()), encoding="utf-8")
    monkeypatch.setattr(store, "EXAMPLE_DIR", example_dir)
    monkeypatch.setattr(store, "OUTPUT_DIR", output_dir)
    return example_dir, output_dir


def test_absent_examples_report_how_to_create_them(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "EXAMPLE_DIR", tmp_path / "nothing-here")
    index = store.load_index()
    assert index["available"] is False
    assert index["examples"] == []
    assert "build_examples.py" in index["hint"]


def test_index_marks_each_example_loadable_and_fresh(baked):
    index = store.load_index()
    assert index["available"] is True
    entry = index["examples"][0]
    assert entry["loadable"] is True
    assert entry["evidence_ok"] is True
    assert entry["routed_task"] == "single_caption"


def test_missing_rendered_evidence_is_flagged_not_hidden(baked):
    example_dir, output_dir = baked
    (output_dir / "ex_preview.png").unlink()
    index = store.load_index()
    entry = index["examples"][0]
    assert entry["loadable"] is True
    assert entry["evidence_ok"] is False
    assert "build_examples.py" in index["hint"]


def test_record_is_served_in_the_job_shape(baked):
    job = store.as_job(store.load_record("ps1-scene-description"))
    # The GUI renders jobs; an example must be indistinguishable in shape.
    for key in ("id", "status", "query", "result", "trace"):
        assert key in job, key
    assert job["is_example"] is True
    assert job["example"]["slug"] == "ps1-scene-description"
    assert job["trace"]["task"] == job["result"]["task"]


def test_unknown_slug_returns_none(baked):
    assert store.load_record("no-such-example") is None
    assert store.load_record("index") is None          # the index is not a record


def test_slug_cannot_escape_the_example_directory(baked):
    assert store.load_record("../../config") is None


def test_endpoints_serve_and_404(baked):
    with TestClient(app) as client:
        index = client.get("/api/examples").json()
        assert index["available"] is True

        job = client.get("/api/examples/ps1-scene-description").json()
        assert job["is_example"] is True
        assert job["result"]["text"]

        missing = client.get("/api/examples/not-baked")
        assert missing.status_code == 404
        assert "build_examples.py" in missing.json()["detail"]


def test_bake_script_covers_the_five_queries_and_the_edge_cases():
    """The curated set is part of the deliverable, so assert its shape."""
    from scripts.build_examples import CASES

    slugs = [c[0] for c in CASES]
    assert len([s for s in slugs if s.startswith("ps")]) == 5
    expected = [c[5] for c in CASES]
    assert {"single_caption", "single_grounding", "change_description",
            "sar_optical_fusion", "change_vqa", "rejected"} <= set(expected)
    for slug, title, subtitle, files, query, task, highlight in CASES:
        assert 1 <= len(files) <= 2, slug
        assert query.strip() and title.strip() and highlight.strip(), slug
