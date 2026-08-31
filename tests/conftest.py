"""Shared fixtures.

Scenes come from ``scripts/make_demo_data``: the same generator that produces the
demo GeoTIFFs also produces the in-memory fixtures, so a test and a demo can never
disagree about what the ground truth is.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.preprocessing.pipeline import prepared_from_array  # noqa: E402
from scripts.make_demo_data import (BARE, BUILT, CRS_A, PIXEL_HA, PIXEL_M, S2_BAND_NAMES,  # noqa: E402
                                    VEGETATION, WATER, base_scene, evolve_scene,
                                    optical_stack, sar_stack)

S2_ROLES = {"blue": 1, "green": 2, "red": 3, "rededge": 4, "nir": 7, "swir1": 10, "swir2": 11}
MX4_ROLES = {"blue": 0, "green": 1, "red": 2, "nir": 3}
S1_DUAL_ROLES = {"vv": 0, "vh": 1}


def make_optical(labels, seed: int = 5, cloud: bool = False, bands: int = 12,
                 crs: str = CRS_A, date: str = "2023-01-01", name: str = "optical"):
    stack, cloud_mask = optical_stack(labels, seed=seed, cloud=cloud, bands=bands)
    roles = S2_ROLES if bands == 12 else MX4_ROLES
    img = prepared_from_array(stack.astype(np.float32), "multispectral", roles=roles, name=name,
                              pixel_size=[PIXEL_M, PIXEL_M], crs=crs, acquisition_date=date)
    return img, cloud_mask


def make_sar(labels, seed: int = 21, dual_pol: bool = False, as_db: bool = False,
             crs: str = CRS_A, date: str = "2023-01-01", name: str = "sar"):
    stack = sar_stack(labels, seed=seed, dual_pol=dual_pol, as_db=as_db)
    roles = S1_DUAL_ROLES if dual_pol else {"amplitude": 0}
    return prepared_from_array(stack.astype(np.float32), "sar", roles=roles, name=name,
                               pixel_size=[PIXEL_M, PIXEL_M], crs=crs, acquisition_date=date,
                               speckle_filter=True)


@pytest.fixture(scope="session")
def labels_t1():
    return base_scene()


@pytest.fixture(scope="session")
def labels_t2(labels_t1):
    return evolve_scene(labels_t1)[0]


@pytest.fixture(scope="session")
def change_truth(labels_t1):
    return evolve_scene(labels_t1)[1]


@pytest.fixture(scope="session")
def optical_t1(labels_t1):
    return make_optical(labels_t1, seed=5, date="2023-01-01", name="areaA_t1")[0]


@pytest.fixture(scope="session")
def optical_t2(labels_t2):
    return make_optical(labels_t2, seed=6, date="2023-07-01", name="areaA_t2")[0]


@pytest.fixture(scope="session")
def optical_rgb_only(labels_t1):
    """3-band visible-only product: exercises the degraded index path."""
    stack, _ = optical_stack(labels_t1, seed=5)
    rgb = np.stack([stack[S2_BAND_NAMES.index(b)] for b in ("B04", "B03", "B02")], axis=0)
    return prepared_from_array(rgb.astype(np.float32), "optical",
                               roles={"red": 0, "green": 1, "blue": 2}, name="rgb_only")


@pytest.fixture(scope="session")
def sar_single(labels_t1):
    return make_sar(labels_t1, seed=21, dual_pol=False, name="areaB_sar")


@pytest.fixture(scope="session")
def sar_dual_db(labels_t1):
    return make_sar(labels_t1, seed=22, dual_pol=True, as_db=True, name="areaB_sar_dualpol")


@pytest.fixture(scope="session")
def optical_with_cloud(labels_t1):
    img, cloud = make_optical(labels_t1, seed=17, cloud=True, bands=4, name="areaB_optical")
    return img, cloud


def class_fraction(labels, class_id: int) -> float:
    return float((labels == class_id).mean())


def close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol
