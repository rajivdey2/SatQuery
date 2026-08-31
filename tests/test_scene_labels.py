"""The adapted BigEarthNet-MM head: feature contract, training, and inference wiring."""
from __future__ import annotations

import json

import numpy as np
import pytest

from backend.analysis import scene_labels
from backend.analysis.landcover import measure_land_cover


def test_feature_spec_is_stable_and_named():
    assert scene_labels.FEATURE_DIM == len(scene_labels.FEATURE_NAMES)
    assert scene_labels.FEATURE_NAMES == scene_labels.feature_names()
    assert len(set(scene_labels.FEATURE_NAMES)) == scene_labels.FEATURE_DIM
    assert any(n.startswith("opt.NDVI") for n in scene_labels.FEATURE_NAMES)
    assert any(n.startswith("sar.SAR_DB") for n in scene_labels.FEATURE_NAMES)


def test_features_have_the_declared_length_for_every_branch_combination(optical_t1, sar_single):
    both = scene_labels.extract_features(optical=optical_t1, sar=sar_single)
    optical_only = scene_labels.extract_features(optical=optical_t1)
    sar_only = scene_labels.extract_features(sar=sar_single)
    for vec in (both, optical_only, sar_only):
        assert vec.shape == (scene_labels.FEATURE_DIM,)
        assert np.all(np.isfinite(vec))


def test_presence_flags_mark_the_missing_branch(optical_t1, sar_single):
    names = scene_labels.FEATURE_NAMES
    sar_flag = names.index("sar.SAR_DB.present")
    opt_flag = names.index("opt.NDVI.present")
    optical_only = scene_labels.extract_features(optical=optical_t1)
    sar_only = scene_labels.extract_features(sar=sar_single)
    assert optical_only[sar_flag] == 0.0 and optical_only[opt_flag] == 1.0
    assert sar_only[sar_flag] == 1.0 and sar_only[opt_flag] == 0.0


def test_rgb_product_reports_missing_spectral_features(optical_rgb_only):
    names = scene_labels.FEATURE_NAMES
    vec = scene_labels.extract_features(optical=optical_rgb_only)
    assert vec[names.index("opt.NDVI.present")] == 0.0
    assert vec[names.index("band.swir1.present")] == 0.0
    assert vec[names.index("band.red.present")] == 1.0


def test_no_head_means_no_labels_are_invented(monkeypatch, optical_t1):
    monkeypatch.setattr(scene_labels.settings, "ben_head_path", "does/not/exist.npz")
    scene_labels._cache.clear()
    preds, source = scene_labels.predict(optical=optical_t1)
    assert preds == [] and source is None
    assert scene_labels.describe()["available"] is False
    measurement = measure_land_cover(optical_t1).measurement
    assert measurement.scene_labels == []
    assert measurement.label_source is None


def test_trainer_learns_synthetic_structure():
    from training.adapt_ben_mm import micro_f1, self_test, train_head

    assert self_test(seed=5, n=900) == 0


def test_trained_head_round_trips_through_the_serving_path(tmp_path, monkeypatch, optical_t1,
                                                          sar_single):
    from training.adapt_ben_mm import BEN19, save_head, train_head

    rng = np.random.default_rng(11)
    n = 400
    dim, classes = scene_labels.FEATURE_DIM, len(BEN19)
    weights = rng.standard_normal((dim, classes)).astype(np.float32) * 0.5
    x = rng.standard_normal((n, dim)).astype(np.float32)
    y = (x @ weights > 0.4).astype(np.float32)
    head, metrics = train_head(x, y, hidden=32, epochs=12, verbose=False)
    path = tmp_path / "ben_mm_lc.npz"
    save_head(path, head, metrics, {"model_id": "ben_mm_lc_test", "dataset": "synthetic"})

    monkeypatch.setattr(scene_labels.settings, "ben_head_path", str(path))
    scene_labels._cache.clear()
    described = scene_labels.describe()
    assert described["available"] is True
    assert described["classes"] == len(BEN19)

    preds, source = scene_labels.predict(optical=optical_t1, sar=sar_single)
    assert len(preds) == 6
    assert all(0.0 <= p["probability"] <= 1.0 for p in preds)
    assert preds == sorted(preds, key=lambda p: -p["probability"])
    assert "optical+SAR" in source
    margin = scene_labels.label_margin(preds)
    assert margin is not None and margin >= 0.0

    measurement = measure_land_cover(optical_t1).measurement
    assert measurement.scene_labels
    assert measurement.label_source
    scene_labels._cache.clear()


def test_head_with_a_stale_feature_spec_is_refused(tmp_path, monkeypatch, optical_t1, capsys):
    path = tmp_path / "stale.npz"
    np.savez(path, w1=np.zeros((3, 2), np.float32), b1=np.zeros(2, np.float32),
             w2=np.zeros((2, 2), np.float32), b2=np.zeros(2, np.float32),
             feature_mean=np.zeros(3, np.float32), feature_std=np.ones(3, np.float32),
             thresholds=np.full(2, 0.5, np.float32),
             classes=np.array(["a", "b"]), feature_names=np.array(["x", "y", "z"]))
    monkeypatch.setattr(scene_labels.settings, "ben_head_path", str(path))
    scene_labels._cache.clear()
    assert scene_labels.load_head() is None
    assert "different feature spec" in capsys.readouterr().out
    scene_labels._cache.clear()


def test_label_encoding_matches_the_nomenclature():
    from training.adapt_ben_mm import BEN19, encode_labels

    y = encode_labels(["Inland waters", "Broad-leaved forest"])
    assert y.sum() == 2
    assert y[BEN19.index("Inland waters")] == 1.0
    assert y[BEN19.index("Broad-leaved forest")] == 1.0
    assert encode_labels(["Not A Real Class"]).sum() == 0


def test_metrics_are_reported_on_a_held_out_split():
    from training.adapt_ben_mm import train_head

    rng = np.random.default_rng(3)
    x = rng.standard_normal((300, scene_labels.FEATURE_DIM)).astype(np.float32)
    y = (rng.random((300, 19)) > 0.8).astype(np.float32)
    _, metrics = train_head(x, y, hidden=16, epochs=6, verbose=False)
    assert metrics["held_out"] is True
    assert metrics["n_val"] > 0 and metrics["n_train"] > metrics["n_val"]
    assert {"micro_f1", "macro_f1", "mean_average_precision", "per_class"} <= set(metrics)
    assert len(metrics["per_class"]) == 19
