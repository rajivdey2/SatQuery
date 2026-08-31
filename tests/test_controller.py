"""Controller: validation gates, routing decisions, and the end-to-end trace.

The problem statement grades the observable execution trace, so these tests assert
on the trace: which task was selected, which registry entry ran, which parameters
were permitted, which were rejected, and what the confidence claims.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from backend.controller.audit import AUDIT_SCHEMA_VERSION, AuditTrace
from backend.controller.classifier import classify
from backend.controller.confidence import estimate, rejection_confidence
from backend.controller.executor import run_pipeline
from backend.controller.registry import (REGISTRY, describe_registry, entry_for_task,
                                         required_input_matches, resolve_params)
from backend.controller.validator import validate_inputs
from scripts.make_demo_data import (CRS_A, EXTENT_A, S2_BAND_NAMES, base_scene, evolve_scene,
                                    optical_stack, sar_stack, write_geotiff)

PS_QUERIES = {
    "Describe the land-cover and major objects visible in this image.": "single_caption",
    "Highlight the water body referred to in the query.": "single_grounding",
    "What changed between these two dates, and where did the change occur?": "change_description",
    "Use the optical and SAR images together to identify built-up and water-covered regions.":
        "sar_optical_fusion",
    "Has the built-up area increased, decreased, or remained unchanged?": "change_vqa",
}


# --------------------------------------------------------------------------- #
# Test rasters on disk (the controller works from files, not arrays)
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def scene_files(tmp_path_factory):
    root = tmp_path_factory.mktemp("scenes")
    labels_t1 = base_scene()
    labels_t2 = evolve_scene(labels_t1)[0]

    paths = {}
    for name, labels, date, seed in (("areaA_20230101.tif", labels_t1, "2023-01-01T04:30:00Z", 5),
                                     ("areaA_20230701.tif", labels_t2, "2023-07-01T04:31:00Z", 6)):
        stack, _ = optical_stack(labels, seed=seed)
        path = root / name
        write_geotiff(path, stack, CRS_A, EXTENT_A, S2_BAND_NAMES,
                      {"ACQUISITION_DATE": date, "SENSOR": "Sentinel-2 MSI (synthetic)"})
        paths[name] = str(path)

    optical_b, _ = optical_stack(labels_t1, seed=17, cloud=True, bands=4)
    path = root / "areaB_optical.tif"
    write_geotiff(path, optical_b, CRS_A, EXTENT_A, ["blue", "green", "red", "nir"],
                  {"ACQUISITION_DATE": "2023-03-15T05:10:00Z",
                   "SENSOR": "Cartosat-2S MX (synthetic)"})
    paths["areaB_optical.tif"] = str(path)

    path = root / "areaB_sar.tif"
    write_geotiff(path, sar_stack(labels_t1, seed=21), CRS_A, EXTENT_A, ["amplitude"],
                  {"ACQUISITION_DATE": "2023-03-15T05:12:00Z", "SENSOR": "RISAT-1 (synthetic)"})
    paths["areaB_sar.tif"] = str(path)

    # Different CRS and footprint: must be rejected when paired with areaA.
    path = root / "areaD_mismatch.tif"
    write_geotiff(path, optical_stack(base_scene(seed=71), seed=41)[0], "EPSG:32643",
                  (410000.0, 2000000.0, 413840.0, 2003840.0), S2_BAND_NAMES,
                  {"ACQUISITION_DATE": "2023-02-02T05:00:00Z"})
    paths["areaD_mismatch.tif"] = str(path)

    # Same date on both images: a bi-temporal pair must be refused.
    path = root / "areaA_sameday.tif"
    stack, _ = optical_stack(labels_t2, seed=8)
    write_geotiff(path, stack, CRS_A, EXTENT_A, S2_BAND_NAMES,
                  {"ACQUISITION_DATE": "2023-01-01T04:30:00Z"})
    paths["areaA_sameday.tif"] = str(path)

    # Unlabelled single-band SAR: exercises the statistical modality sniffing.
    path = root / "areaC_unlabelled.tif"
    write_geotiff(path, sar_stack(labels_t1, seed=33), CRS_A, EXTENT_A, [""], {})
    paths["areaC_unlabelled.tif"] = str(path)

    # A tiny raster, below the analysable size floor.
    path = root / "tiny.tif"
    write_geotiff(path, np.zeros((3, 8, 8), np.int16), CRS_A,
                  (523000.0, 3120000.0, 523080.0, 3120080.0), ["red", "green", "blue"], {})
    paths["tiny.tif"] = str(path)
    return paths


# --------------------------------------------------------------------------- #
# Validator
# --------------------------------------------------------------------------- #

def test_single_geotiff_passes_validation(scene_files):
    report = validate_inputs([scene_files["areaA_20230101.tif"]], "describe this image")
    assert report.accepted
    assert report.input_config.modality_signature == ["multispectral"]
    names = {c.name for c in report.checks}
    assert {"format", "modality", "georeference", "metadata", "input_configuration"} <= names


def test_bitemporal_pair_is_recognised_with_dates(scene_files):
    report = validate_inputs([scene_files["areaA_20230101.tif"], scene_files["areaA_20230701.tif"]])
    assert report.accepted
    assert report.input_config.bi_temporal is True
    assert report.input_config.dates_differ is True
    assert report.input_config.co_registered is True


def test_cross_modal_pair_is_recognised(scene_files):
    report = validate_inputs([scene_files["areaB_optical.tif"], scene_files["areaB_sar.tif"]])
    assert report.accepted
    assert report.input_config.cross_modal is True
    assert "sar" in report.input_config.modality_signature


def test_mismatched_footprints_are_rejected_with_an_explanation(scene_files):
    report = validate_inputs([scene_files["areaA_20230101.tif"], scene_files["areaD_mismatch.tif"]])
    assert not report.accepted
    assert report.input_config.co_registered is False
    assert any("overlap" in f for f in report.failed)
    assert any(c.name == "co_registration" and c.status == "failed" for c in report.checks)


def test_identical_dates_are_rejected_for_a_bitemporal_pair(scene_files):
    report = validate_inputs([scene_files["areaA_20230101.tif"], scene_files["areaA_sameday.tif"]])
    assert not report.accepted
    assert any("same acquisition date" in f or "acquisition date" in f for f in report.failed)


def test_too_many_images_are_rejected(scene_files):
    files = [scene_files["areaA_20230101.tif"]] * 3
    report = validate_inputs(files)
    assert not report.accepted
    assert any("at most" in f for f in report.failed)


def test_tiny_raster_is_rejected(scene_files):
    report = validate_inputs([scene_files["tiny.tif"]])
    assert not report.accepted
    assert any("too small" in f or "at least" in f for f in report.failed)


def test_unlabelled_sar_is_sniffed_statistically(scene_files):
    report = validate_inputs([scene_files["areaC_unlabelled.tif"]])
    check = next(c for c in report.checks if c.name == "modality")
    assert check.detail["modality"] in ("sar", "unknown", "panchromatic")
    assert check.detail["confidence"] in ("medium", "low")
    if check.detail["modality"] == "sar":
        assert "speckle" in check.message


def test_png_is_accepted_but_flagged_as_benchmark_only(tmp_path):
    from PIL import Image

    rgb = (np.random.default_rng(0).random((96, 96, 3)) * 255).astype(np.uint8)
    path = tmp_path / "benchmark.png"
    Image.fromarray(rgb).save(path)
    report = validate_inputs([str(path)])
    assert report.accepted
    assert any("benchmark input only" in w for w in report.warnings)


# --------------------------------------------------------------------------- #
# Classifier
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("query,expected", list(PS_QUERIES.items()))
def test_the_five_problem_statement_queries_route_correctly(query, expected, scene_files):
    if expected.startswith("single"):
        files = [scene_files["areaA_20230101.tif"]]
    elif expected == "sar_optical_fusion":
        files = [scene_files["areaB_optical.tif"], scene_files["areaB_sar.tif"]]
    else:
        files = [scene_files["areaA_20230101.tif"], scene_files["areaA_20230701.tif"]]
    report = validate_inputs(files, query)
    assert report.accepted
    result = classify(query, report.input_config)
    assert result.task == expected, result.to_audit()


def test_change_query_on_a_single_image_falls_back_and_explains(scene_files):
    report = validate_inputs([scene_files["areaA_20230101.tif"]])
    result = classify("What changed between these two dates?", report.input_config)
    assert result.task == "single_vqa"
    assert any("only one image" in r for r in result.reasons)
    assert "change_vqa" in result.infeasible


def test_classifier_records_alternatives_and_infeasible_tasks(scene_files):
    report = validate_inputs([scene_files["areaA_20230101.tif"]])
    audit = classify("Describe this scene.", report.input_config).to_audit()
    assert set(audit["candidate_tasks"]) == {
        "single_vqa", "single_caption", "single_grounding",
        "change_vqa", "change_description", "sar_optical_fusion"}
    assert audit["alternatives"]
    assert sum(audit["alternatives"].values()) == pytest.approx(1.0, abs=1e-6)
    assert set(audit["infeasible_tasks"]) >= {"change_vqa", "sar_optical_fusion"}


def test_fusion_wording_cannot_win_without_a_sar_image(scene_files):
    report = validate_inputs([scene_files["areaA_20230101.tif"], scene_files["areaA_20230701.tif"]])
    result = classify("Use the optical and SAR images together to identify water.",
                      report.input_config)
    assert result.task != "sar_optical_fusion"
    assert "sar_optical_fusion" in result.infeasible


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

def test_registry_covers_every_task_exactly_once():
    tasks = [e.task for e in REGISTRY]
    assert len(tasks) == len(set(tasks)) == 6


def test_unpermitted_parameters_are_rejected_and_named():
    effective, rejected, notes = resolve_params(
        "single_vqa", {"min_region_pixels": 128, "secret_backdoor": 1, "temperature": 9.9})
    assert effective["min_region_pixels"] == 128
    assert "secret_backdoor" in rejected
    assert "temperature" in rejected            # 9.9 exceeds the declared maximum
    assert effective["temperature"] != 9.9
    assert any("not a permitted parameter" in n for n in notes)


def test_target_class_enum_is_enforced():
    _, rejected, _ = resolve_params("single_grounding", {"target_class": "aircraft"})
    assert "target_class" in rejected
    effective, rejected2, _ = resolve_params("single_grounding", {"target_class": "water"})
    assert not rejected2 and effective["target_class"] == "water"


def test_required_inputs_are_enforced_per_entry():
    assert required_input_matches("single_vqa", 1, ["optical"])[0]
    assert not required_input_matches("single_vqa", 2, ["optical", "optical"])[0]
    assert required_input_matches("sar_optical_fusion", 2, ["optical", "sar"])[0]
    assert not required_input_matches("sar_optical_fusion", 2, ["optical", "optical"])[0]
    assert not required_input_matches("change_vqa", 2, ["optical", "sar"])[0]


def test_registry_description_is_serialisable():
    payload = describe_registry()
    json.dumps(payload)                          # must not raise
    assert len(payload["entries"]) == 6
    assert "narration_backend" in payload and "adapted_head" in payload


# --------------------------------------------------------------------------- #
# Confidence
# --------------------------------------------------------------------------- #

def test_confidence_is_unavailable_without_signals():
    conf = estimate({})
    assert conf.value is None
    assert conf.source == "not_available"


def test_confidence_lists_its_components_and_stays_uncalibrated():
    conf = estimate({"separability": 0.9, "band_completeness": 1.0, "valid_pixel_fraction": 1.0})
    assert conf.value is not None
    assert conf.source == "measurement_composite"
    assert conf.calibrated is False
    assert {c.name for c in conf.components} >= {"separability", "band_completeness"}
    assert "not a calibrated probability" in conf.note


def test_cross_modal_agreement_takes_over_as_the_source():
    conf = estimate({"separability": 0.8, "cross_modal_kappa": 0.7})
    assert conf.source == "cross_modal_agreement"


def test_limitations_and_low_reliability_pull_the_score_down():
    strong = estimate({"separability": 0.9, "band_completeness": 1.0})
    weak = estimate({"separability": 0.9, "band_completeness": 1.0,
                     "n_limitations": 4, "target_reliability": "low"})
    assert weak.value < strong.value


def test_mock_mode_reports_no_number():
    conf = estimate({"separability": 0.9}, narration_source="mock")
    assert conf.value is None and conf.source == "mock"


def test_rejection_confidence_is_certain():
    conf = rejection_confidence(["footprints do not overlap"])
    assert conf.value == 1.0 and conf.source == "validator_gate"


# --------------------------------------------------------------------------- #
# End-to-end
# --------------------------------------------------------------------------- #

def _run(files, query, run_id, params=None):
    return run_pipeline(files, query, run_id, model_params=params)


def test_end_to_end_single_caption(scene_files):
    trace, result = _run([scene_files["areaA_20230101.tif"]],
                         "Describe the land-cover and major objects visible in this image.",
                         "test_caption")
    assert result is not None and result.task == "single_caption"
    assert trace.schema_version == AUDIT_SCHEMA_VERSION
    assert [e.id for e in trace.registry_entries_used] == ["rs_vlm.caption"]
    assert trace.steps and any(s.tool == "analysis.land_cover" for s in trace.steps)
    assert trace.measurements["single_caption"]["classes"]
    assert trace.outputs.mask.kind == "class_map"
    assert Path(trace.outputs.mask.path).exists()
    assert trace.confidence.value is not None
    assert "vegetation" in result.text


def test_end_to_end_grounding_produces_boxes_and_an_overlay(scene_files):
    trace, result = _run([scene_files["areaA_20230101.tif"]],
                         "Highlight the water body referred to in the query.", "test_ground")
    assert result.task == "single_grounding"
    assert result.boxes, result.text
    box = result.boxes[0]["bbox"]
    assert len(box) == 4 and all(0 <= v <= 100 for v in box)
    overlay = next(e for e in trace.outputs.evidence if e.role == "grounding_overlay")
    assert Path(overlay.path).exists()


def test_end_to_end_change_description_produces_a_change_map(scene_files):
    trace, result = _run([scene_files["areaA_20230101.tif"], scene_files["areaA_20230701.tif"]],
                         "What changed between these two dates, and where did the change occur?",
                         "test_change")
    assert result.task == "change_description"
    assert trace.outputs.mask.kind == "change_map"
    assert Path(trace.outputs.mask.path).exists()
    block = trace.measurements["change_description"]
    assert block["per_class"] and block["transitions"]
    assert block["magnitude_threshold"] is not None


def test_end_to_end_change_vqa_answers_the_built_up_trend(scene_files):
    trace, result = _run([scene_files["areaA_20230101.tif"], scene_files["areaA_20230701.tif"]],
                         "Has the built-up area increased, decreased, or remained unchanged?",
                         "test_change_vqa")
    assert result.task == "change_vqa"
    assert "increased" in result.text
    assert "ha" in result.text or "percentage points" in result.text


def test_end_to_end_fusion_reports_agreement(scene_files):
    trace, result = _run([scene_files["areaB_optical.tif"], scene_files["areaB_sar.tif"]],
                         "Use the optical and SAR images together to identify built-up and "
                         "water-covered regions.", "test_fusion")
    assert result.task == "sar_optical_fusion"
    assert trace.confidence.source == "cross_modal_agreement"
    block = trace.measurements["sar_optical_fusion"]
    assert block["agreement"]
    assert any(e.role == "fusion_map" for e in trace.outputs.evidence)


def test_fusion_query_asking_where_adds_a_second_registry_entry(scene_files):
    trace, result = _run([scene_files["areaB_optical.tif"], scene_files["areaB_sar.tif"]],
                         "Use the optical and SAR images together and highlight where the water is.",
                         "test_fusion_seq")
    assert len(trace.plan) == 2
    assert trace.plan == ["fusion.joint", "rs_vlm.grounding"]
    assert {e.id for e in trace.registry_entries_used} == {"fusion.joint", "rs_vlm.grounding"}
    assert result.boxes


def test_rejected_pair_produces_a_complete_trace(scene_files):
    trace, result = _run([scene_files["areaA_20230101.tif"], scene_files["areaD_mismatch.tif"]],
                         "What changed between these two dates?", "test_reject")
    assert trace.task == "rejected"
    assert result.task == "rejected"
    assert trace.confidence.source == "validator_gate"
    assert trace.validation.failed
    assert result.rejected["checks"]
    assert not trace.registry_entries_used


def test_permitted_parameters_reach_the_specialist_and_appear_in_the_trace(scene_files):
    trace, _ = _run([scene_files["areaA_20230101.tif"]], "Describe this image.", "test_params",
                    params={"min_region_pixels": 256, "not_a_param": True})
    assert trace.effective_parameters["min_region_pixels"] == 256
    assert "not_a_param" in trace.registry_entries_used[0].rejected_parameters
    land_cover_step = next(s for s in trace.steps if s.tool == "analysis.land_cover")
    assert land_cover_step.parameters["min_region_pixels"] == 256


def test_trace_is_written_to_disk_and_round_trips(scene_files):
    trace, result = _run([scene_files["areaA_20230101.tif"]], "Describe this image.", "test_disk")
    path = Path(result.trace_path)
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == AUDIT_SCHEMA_VERSION
    AuditTrace.model_validate(payload)           # the schema accepts its own output


def test_trace_records_the_environment_and_narration_source(scene_files):
    trace, _ = _run([scene_files["areaA_20230101.tif"]], "Describe this image.", "test_env")
    assert trace.environment["server_version"]
    assert trace.model_backend == "analysis_engine"
    assert trace.outputs.text.narration_source == "measurement"
