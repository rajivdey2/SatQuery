"""Preprocessing: band-role resolution, SAR calibration, geometry."""
from __future__ import annotations

import numpy as np
import pytest

from backend.preprocessing import bands as bandmod
from backend.preprocessing import sar_ops
from backend.preprocessing.pipeline import align_pair, prepared_from_array


def test_band_names_beat_band_count():
    """Explicit band descriptions must win over the count convention."""
    bm = bandmod.resolve_bands(band_count=12, modality="multispectral",
                               descriptions=["B01", "B02", "B03", "B04", "B05", "B06",
                                             "B07", "B08", "B8A", "B09", "B11", "B12"])
    assert bm.source == "descriptions"
    assert bm.idx("red") == 3 and bm.idx("nir") == 7
    assert bm.idx("swir1") == 10 and bm.idx("swir2") == 11


def test_sentinel2_12band_layout_without_descriptions():
    bm = bandmod.resolve_bands(band_count=12, modality="multispectral")
    assert bm.source == "band_count"
    assert bm.has("blue", "green", "red", "nir", "swir1", "swir2")


def test_four_band_order_resolved_from_vegetation_signature():
    """B,G,R,NIR vs R,G,B,NIR is decided by which visible plane opposes NIR."""
    rng = np.random.default_rng(0)
    h = w = 64
    veg = rng.random((h, w)) > 0.5
    blue = np.where(veg, 0.04, 0.16) + rng.normal(0, 0.002, (h, w))
    green = np.where(veg, 0.06, 0.19) + rng.normal(0, 0.002, (h, w))
    red = np.where(veg, 0.035, 0.21) + rng.normal(0, 0.002, (h, w))
    nir = np.where(veg, 0.40, 0.27) + rng.normal(0, 0.002, (h, w))

    bgrn = np.stack([blue, green, red, nir]).astype(np.float32)
    bm = bandmod.resolve_bands(band_count=4, modality="multispectral", raw=bgrn)
    assert bm.idx("red") == 2 and bm.idx("blue") == 0, bm.notes

    rgbn = np.stack([red, green, blue, nir]).astype(np.float32)
    bm2 = bandmod.resolve_bands(band_count=4, modality="multispectral", raw=rgbn)
    assert bm2.idx("red") == 0 and bm2.idx("blue") == 2, bm2.notes


def test_rgb_product_reports_no_nir():
    bm = bandmod.resolve_bands(band_count=3, modality="optical")
    assert bm.has("red", "green", "blue")
    assert not bm.has("nir")


def test_sar_dual_pol_default_and_named():
    assert bandmod.resolve_bands(band_count=2, modality="sar").roles == {"vv": 0, "vh": 1}
    named = bandmod.resolve_bands(band_count=2, modality="sar", descriptions=["VH", "VV"])
    assert named.idx("vh") == 0 and named.idx("vv") == 1


def test_db_products_are_not_relogged():
    db = np.linspace(-25.0, -3.0, 4096).reshape(64, 64).astype(np.float32)
    out, convention = sar_ops.to_db(db)
    assert convention == "already_db"
    assert np.allclose(out, db, atol=1e-4)


def test_amplitude_and_intensity_conventions():
    amp = np.full((64, 64), 400.0, dtype=np.float32)
    out, convention = sar_ops.to_db(amp)
    assert convention == "amplitude"
    assert np.isclose(out[0, 0], 20 * np.log10(400.0), atol=1e-3)

    intensity = np.full((64, 64), 250_000.0, dtype=np.float32)
    out2, convention2 = sar_ops.to_db(intensity)
    assert convention2 == "intensity"
    assert np.isclose(out2[0, 0], 10 * np.log10(250_000.0), atol=1e-3)


def test_uncalibrated_digital_numbers_are_flagged(sar_single):
    """A DN product's dB values sit far from sigma0, and that must be reported."""
    channel = next(iter(sar_single.sar_channels.values()))
    assert channel.convention == "amplitude"
    assert channel.calibrated is False
    assert any("uncalibrated" in note for note in channel.notes)


def test_calibrated_db_product_is_recognised(sar_dual_db):
    channel = sar_dual_db.sar_channels["vv"]
    assert channel.convention == "already_db"
    assert channel.calibrated is True


def test_speckle_filter_reduces_variance_but_keeps_the_mean(labels_t1):
    from scripts.make_demo_data import sar_stack

    raw, _ = sar_ops.to_db(sar_stack(labels_t1, seed=3)[0].astype(np.float32))
    filtered = sar_ops.refined_lee(raw, win=7)
    assert filtered.std() < raw.std()
    assert abs(float(filtered.mean()) - float(raw.mean())) < 1.0


def test_enl_is_lower_for_heavier_speckle():
    rng = np.random.default_rng(1)
    flat = np.full((256, 256), 1.0)
    heavy = flat * rng.gamma(1.0, 1.0, (256, 256))       # 1 look
    light = flat * rng.gamma(16.0, 1.0 / 16.0, (256, 256))
    assert sar_ops.estimate_enl(heavy) < sar_ops.estimate_enl(light)


def test_pixel_area_uses_projected_units(optical_t1):
    assert optical_t1.pixel_area_m2 == pytest.approx(100.0, rel=1e-6)
    assert optical_t1.area_ha(10_000) == pytest.approx(100.0, rel=1e-6)


def test_pixel_area_converts_degrees_to_metres():
    img = prepared_from_array(np.zeros((3, 32, 32), np.float32), "optical",
                              roles={"red": 0, "green": 1, "blue": 2},
                              pixel_size=[0.0001, 0.0001], crs="EPSG:4326")
    # ~11 m at the equator, so an area of roughly 120 m2 per pixel.
    assert 80.0 < img.pixel_area_m2 < 160.0


def test_align_pair_resamples_onto_the_reference_grid(optical_t1, labels_t1):
    small = prepared_from_array(
        np.zeros((12, optical_t1.shape[0] // 2, optical_t1.shape[1] // 2), np.float32),
        "multispectral", name="half")
    aligned = align_pair(optical_t1, small)
    assert aligned.shape == optical_t1.shape
    assert any("Resampled" in n for n in aligned.notes)


def test_display_rgb_uses_true_colour_when_roles_are_known(optical_t1):
    rgb = optical_t1.rgb
    assert rgb.shape == (*optical_t1.shape, 3)
    assert rgb.dtype == np.uint8
