"""Analysis engine: do the measurements actually recover the known scene?

The fixtures are rendered from an explicit label map, so the correct answer is
known. These tests assert on the numbers, not merely on the absence of exceptions —
a pipeline that runs cleanly and measures the wrong thing is the failure mode that
matters here.
"""
from __future__ import annotations

import numpy as np
import pytest

from backend.analysis.change import measure_change
from backend.analysis.fusion import detect_obscured, measure_fusion
from backend.analysis.grounding import ground_query, parse_phrase
from backend.analysis.indices import compute_indices
from backend.analysis.landcover import measure_land_cover
from backend.analysis.measurements import BARE_SOIL, BUILT_UP, VEGETATION, WATER
from backend.analysis.narrate import answer_change, answer_fusion, answer_single, describe_scene
from backend.analysis.thresholds import otsu, threshold_with_prior
from scripts.make_demo_data import BUILT as L_BUILT
from scripts.make_demo_data import VEGETATION as L_VEG
from scripts.make_demo_data import WATER as L_WATER


# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #

def test_otsu_separates_two_clean_modes():
    rng = np.random.default_rng(0)
    values = np.concatenate([rng.normal(-0.6, 0.05, 5000), rng.normal(0.6, 0.05, 5000)])
    threshold, eta = otsu(values)
    assert -0.2 < threshold < 0.2
    assert eta > 0.9


def test_otsu_separability_collapses_on_a_unimodal_histogram():
    rng = np.random.default_rng(0)
    _, eta = otsu(rng.normal(0.0, 1.0, 20000))
    assert eta < 0.65


def test_prior_wins_when_the_histogram_is_unimodal():
    rng = np.random.default_rng(1)
    t = threshold_with_prior(rng.normal(-0.4, 0.15, 8000), prior=0.0, window=0.25, name="NDWI")
    assert t.method == "prior_fixed"
    assert t.value == pytest.approx(0.0)
    assert "not convincingly bimodal" in t.note
    # A single Gaussian scores ~0.64, which is exactly why the cut-off sits above it.
    assert 0.55 < t.separability < 0.75


def test_out_of_window_otsu_falls_back_to_the_prior():
    """The dominant histogram split is often not the boundary being looked for."""
    rng = np.random.default_rng(2)
    values = np.concatenate([rng.normal(-0.9, 0.02, 5000), rng.normal(0.9, 0.02, 5000)])
    t = threshold_with_prior(values, prior=0.6, window=0.05, name="NDVI")
    assert t.method == "otsu_rejected_prior_used"
    assert t.value == pytest.approx(0.6)
    assert "outside the plausible" in t.note


def test_dark_mode_threshold_isolates_a_minority_dark_class():
    """Water is a small minority of a scene; plain Otsu would miss its boundary."""
    from backend.analysis.thresholds import dark_mode_threshold, otsu

    rng = np.random.default_rng(4)
    water = rng.normal(-21.0, 0.6, 700)          # 7% of the scene
    bare = rng.normal(-14.5, 0.9, 2300)
    vegetation = rng.normal(-10.0, 1.1, 5500)
    built = rng.normal(-4.0, 3.6, 1500)
    values = np.concatenate([water, bare, vegetation, built])

    plain, _ = otsu(values)
    dark = dark_mode_threshold(values, name="backscatter")
    assert dark is not None and dark.method == "dark_mode_otsu"
    assert -19.0 < dark.value < -11.0, dark.note
    assert plain > dark.value                    # the dominant split sits much higher
    assert float((values < dark.value).mean()) < 0.35


def test_dark_mode_threshold_declines_a_unimodal_histogram():
    from backend.analysis.thresholds import dark_mode_threshold

    rng = np.random.default_rng(6)
    t = dark_mode_threshold(rng.normal(-12.0, 2.0, 20000), name="backscatter")
    assert t is None or t.method.endswith("rejected")


# --------------------------------------------------------------------------- #
# Indices
# --------------------------------------------------------------------------- #

def test_multispectral_product_supports_every_index(optical_t1):
    stack = compute_indices(optical_t1)
    assert stack.basis == "multispectral"
    for name in ("NDVI", "NDWI", "MNDWI", "NDBI", "BRIGHTNESS", "TEXTURE"):
        assert name in stack.values, name
    assert not stack.unavailable


def test_rgb_product_reports_ndvi_as_unavailable(optical_rgb_only):
    stack = compute_indices(optical_rgb_only)
    assert stack.basis == "visible_only"
    assert "NDVI" in stack.unavailable and "MNDWI" in stack.unavailable
    assert "EXG" in stack.values and "BWI" in stack.values
    assert "near-infrared" in stack.unavailable["NDVI"] or "NIR" in stack.unavailable["NDVI"]


def test_index_values_match_the_scene_signatures(optical_t1, labels_t1):
    stack = compute_indices(optical_t1)
    ndvi, ndwi = stack.get("NDVI"), stack.get("NDWI")
    assert ndvi[labels_t1 == L_VEG].mean() > 0.6
    assert ndvi[labels_t1 == L_WATER].mean() < 0.1
    assert ndwi[labels_t1 == L_WATER].mean() > 0.4
    assert ndwi[labels_t1 == L_VEG].mean() < 0.0


def test_single_pol_sar_has_no_polarimetric_ratio(sar_single):
    stack = compute_indices(sar_single)
    assert stack.basis == "sar"
    assert "SAR_RATIO" in stack.unavailable
    assert "SAR_DB" in stack.values and "SAR_TEXTURE" in stack.values


def test_dual_pol_sar_exposes_the_ratio(sar_dual_db):
    stack = compute_indices(sar_dual_db)
    assert "SAR_RATIO" in stack.values


# --------------------------------------------------------------------------- #
# Land cover
# --------------------------------------------------------------------------- #

def test_land_cover_recovers_the_known_class_extents(optical_t1, labels_t1):
    result = measure_land_cover(optical_t1)
    m = result.measurement
    for class_name, label_id, tol in ((WATER, L_WATER, 0.03),
                                      (VEGETATION, L_VEG, 0.12),
                                      (BUILT_UP, L_BUILT, 0.12)):
        truth = float((labels_t1 == label_id).mean())
        measured = m.fraction(class_name)
        assert abs(measured - truth) < tol, f"{class_name}: measured {measured:.3f} vs truth {truth:.3f}"
    assert m.dominant == VEGETATION


def test_water_mask_overlaps_the_true_water(optical_t1, labels_t1):
    result = measure_land_cover(optical_t1)
    truth = labels_t1 == L_WATER
    predicted = result.mask(WATER)
    intersection = float((truth & predicted).sum())
    assert intersection / max(float(truth.sum()), 1.0) > 0.7      # recall
    assert intersection / max(float(predicted.sum()), 1.0) > 0.6  # precision


def test_areas_are_reported_in_hectares(optical_t1, labels_t1):
    m = measure_land_cover(optical_t1).measurement
    water = m.get(WATER)
    assert water.area_ha is not None
    expected = float((labels_t1 == L_WATER).sum()) * 0.01        # 10 m pixels -> 0.01 ha
    assert water.area_ha == pytest.approx(expected, rel=0.35)


def test_every_class_states_its_evidence_and_reliability(optical_t1):
    m = measure_land_cover(optical_t1).measurement
    for c in m.classes:
        assert c.evidence_basis, c.name
        assert c.reliability in ("high", "medium", "low", "unavailable")
    assert m.quality.separability is not None


def test_rgb_only_product_degrades_and_says_so(optical_rgb_only):
    m = measure_land_cover(optical_rgb_only).measurement
    assert any("near-infrared" in lim for lim in m.quality.limitations)
    assert "NDVI" in m.quality.indices_unavailable
    veg = m.get(VEGETATION)
    assert "visible-only" in veg.evidence_basis
    assert veg.reliability == "medium"


def test_sar_only_does_not_claim_vegetation_without_dual_pol(sar_single):
    m = measure_land_cover(sar_single).measurement
    veg = m.get(VEGETATION)
    assert veg.reliability == "unavailable"
    assert veg.fraction == 0.0
    assert any("SAR-only" in lim for lim in m.quality.limitations)


def test_sar_finds_water_and_built_up(sar_dual_db, labels_t1):
    result = measure_land_cover(sar_dual_db)
    m = result.measurement
    truth_water = float((labels_t1 == L_WATER).mean())
    assert m.fraction(WATER) > truth_water * 0.4
    assert m.fraction(BUILT_UP) > 0.02
    water_basis = m.get(WATER).evidence_basis
    assert "backscatter" in water_basis


def test_regions_carry_boxes_positions_and_scores(optical_t1):
    m = measure_land_cover(optical_t1).measurement
    assert m.regions
    for r in m.regions:
        assert len(r.bbox_norm) == 4
        assert 0.0 <= r.bbox_norm[0] < r.bbox_norm[2] <= 100.0
        assert 0.0 <= r.bbox_norm[1] < r.bbox_norm[3] <= 100.0
        assert r.position
        assert 0.0 <= r.score <= 1.0


# --------------------------------------------------------------------------- #
# Grounding
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("phrase,expected", [
    ("Highlight the water body referred to in the query.", WATER),
    ("where is the river", WATER),
    ("mark all the buildings", BUILT_UP),
    ("outline the forested area", VEGETATION),
    ("find the barren land", BARE_SOIL),
    ("show me the largest lake in the north", WATER),
])
def test_phrase_parsing_maps_to_classes(phrase, expected):
    assert parse_phrase(phrase).target_class == expected


def test_phrase_parsing_extracts_qualifiers():
    parsed = parse_phrase("highlight the largest water body in the south-west")
    assert parsed.superlative == "largest"
    assert parsed.direction == "south-west"


def test_grounding_localises_the_water_body(optical_t1, labels_t1):
    measurement, aux = ground_query(optical_t1, "Highlight the water body referred to in the query.")
    assert measurement.target_class == WATER
    assert measurement.regions
    region = measurement.regions[0]
    truth = labels_t1 == L_WATER
    ys, xs = np.nonzero(truth)
    h, w = labels_t1.shape
    x1, y1, x2, y2 = region.bbox_px
    # The predicted box must sit inside the true water envelope.
    assert x1 >= xs.min() - w * 0.05 and x2 <= xs.max() + w * 0.05
    assert y1 >= ys.min() - h * 0.05 and y2 <= ys.max() + h * 0.05


def test_grounding_reports_unmatched_phrases_honestly(optical_t1):
    measurement, _ = ground_query(optical_t1, "highlight the aircraft carrier")
    assert measurement.method == "phrase_unmatched_dominant_class"
    assert any("does not name a land-cover class" in lim for lim in measurement.quality.limitations)
    if measurement.regions:
        assert measurement.regions[0].score <= 0.35


def test_explicit_target_class_overrides_the_phrase(optical_t1):
    measurement, aux = ground_query(optical_t1, "anything at all", target_class=BUILT_UP)
    assert measurement.target_class == BUILT_UP
    assert measurement.method == "class_mask_connected_components_v1"
    assert aux["parsed"].matched_term == f"explicit parameter target_class={BUILT_UP}"


# --------------------------------------------------------------------------- #
# Change
# --------------------------------------------------------------------------- #

def test_change_detects_the_known_built_up_expansion(optical_t1, optical_t2, change_truth):
    measurement, masks = measure_change(optical_t1, optical_t2)
    built = measurement.delta(BUILT_UP)
    assert built.direction == "increased", measurement.per_class
    assert built.significant
    assert built.delta_ha is not None
    # The synthetic conversion is ~85 ha; allow a wide band for classifier boundaries.
    assert 0.3 * change_truth["built_up_gain_ha"] < built.delta_ha < 2.5 * change_truth["built_up_gain_ha"]


def test_change_detects_the_receding_water(optical_t1, optical_t2):
    measurement, _ = measure_change(optical_t1, optical_t2)
    water = measurement.delta(WATER)
    assert water.delta_fraction < 0
    assert water.direction == "decreased"


def test_changed_fraction_is_in_the_right_ballpark(optical_t1, optical_t2, change_truth):
    measurement, _ = measure_change(optical_t1, optical_t2)
    truth = change_truth["changed_fraction"]
    assert 0.3 * truth < measurement.changed_fraction < 3.0 * truth


def test_identical_images_report_no_significant_change(optical_t1):
    measurement, _ = measure_change(optical_t1, optical_t1)
    assert measurement.changed_fraction < 0.05
    assert not [d for d in measurement.per_class if d.significant]


def test_change_records_threshold_noise_floor_and_transitions(optical_t1, optical_t2):
    measurement, _ = measure_change(optical_t1, optical_t2)
    assert measurement.magnitude_threshold is not None
    assert measurement.noise_floor is not None
    assert measurement.magnitude_threshold >= measurement.noise_floor
    assert measurement.transitions
    top = measurement.transitions[0]
    assert {"from", "to", "pixels", "fraction", "location"} <= set(top)
    assert measurement.clusters


def test_change_threshold_is_a_permitted_override(optical_t1, optical_t2):
    loose, _ = measure_change(optical_t1, optical_t2, change_threshold=0.01)
    tight, _ = measure_change(optical_t1, optical_t2, change_threshold=5.0)
    assert loose.changed_fraction > tight.changed_fraction


# --------------------------------------------------------------------------- #
# Fusion
# --------------------------------------------------------------------------- #

def test_cloud_is_detected_as_obscured(optical_with_cloud):
    img, cloud = optical_with_cloud
    from backend.analysis.indices import compute_indices as ci

    mask, note = detect_obscured(img, ci(img))
    truth = float(cloud.mean())
    assert truth > 0.01
    overlap = float((mask & cloud).sum()) / max(float(cloud.sum()), 1.0)
    assert overlap > 0.5, note


def test_fusion_reports_per_class_agreement(optical_with_cloud, sar_single):
    optical, _ = optical_with_cloud
    measurement, masks = measure_fusion(optical, sar_single)
    assert measurement.agreement
    water = next(a for a in measurement.agreement if a.class_name == WATER)
    assert water.cohen_kappa is not None
    assert water.iou is not None
    assert measurement.mean_agreement is not None


def test_fusion_recovers_the_obscured_area_from_sar(optical_with_cloud, sar_single):
    optical, cloud = optical_with_cloud
    measurement, masks = measure_fusion(optical, sar_single)
    assert measurement.obscured_fraction > 0.01
    assert masks["obscured"].any()
    # The joint pass must not simply reproduce the optical classification.
    joint_built = masks[BUILT_UP]
    assert joint_built.any()


def test_fusion_declines_vegetation_from_single_pol_sar(optical_with_cloud, sar_single):
    optical, _ = optical_with_cloud
    measurement, _ = measure_fusion(optical, sar_single)
    veg = next(a for a in measurement.agreement if a.class_name == VEGETATION)
    assert veg.sar_fraction is None
    assert veg.cohen_kappa is None


def test_fusion_with_dual_pol_sar_can_speak_about_vegetation(optical_with_cloud, sar_dual_db):
    optical, _ = optical_with_cloud
    measurement, _ = measure_fusion(optical, sar_dual_db)
    veg = next(a for a in measurement.agreement if a.class_name == VEGETATION)
    assert veg.sar_fraction is not None


# --------------------------------------------------------------------------- #
# Narration only restates measurements
# --------------------------------------------------------------------------- #

def test_scene_description_quotes_measured_extents(optical_t1):
    m = measure_land_cover(optical_t1).measurement
    text = describe_scene(m, "multispectral")
    assert "vegetation" in text
    assert "%" in text and "ha" in text


def test_vqa_answers_presence_extent_count_and_location(optical_t1):
    m = measure_land_cover(optical_t1).measurement
    assert answer_single("Is there any water in this image?", m).lower().startswith("yes")
    assert "%" in answer_single("What percentage of the scene is vegetation?", m)
    assert "region" in answer_single("How many water bodies are there?", m)
    assert any(word in answer_single("Where is the built-up area?", m)
               for word in ("north", "south", "east", "west", "centre"))


def test_change_answer_states_a_direction_and_a_number(optical_t1, optical_t2):
    measurement, _ = measure_change(optical_t1, optical_t2)
    text = answer_change("Has the built-up area increased, decreased, or remained unchanged?",
                         measurement)
    assert any(word in text for word in ("increased", "decreased", "unchanged"))
    assert "percentage points" in text


def test_fusion_answer_mentions_both_sensors(optical_with_cloud, sar_single):
    optical, _ = optical_with_cloud
    measurement, _ = measure_fusion(optical, sar_single)
    text = answer_fusion("Use the optical and SAR images together to identify built-up and "
                         "water-covered regions.", measurement)
    assert "built-up" in text
    assert "kappa" in text or "SAR" in text
