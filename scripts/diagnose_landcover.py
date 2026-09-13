"""Ad-hoc diagnostic: what does the land-cover classifier actually decide, and why?

Prints the per-class truth vs measurement plus every threshold and its provenance,
for the synthetic scene whose ground truth is known. Not a test -- a debugging aid.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.analysis.indices import compute_indices
from backend.analysis.landcover import measure_land_cover
from backend.analysis.measurements import CLASS_ORDER
from backend.preprocessing.pipeline import prepared_from_array
from scripts.make_demo_data import (BARE, BUILT, CRS_A, PIXEL_M, VEGETATION, WATER,
                                    base_scene, optical_stack)

S2_ROLES = {"blue": 1, "green": 2, "red": 3, "rededge": 4, "nir": 7, "swir1": 10, "swir2": 11}
TRUTH_ID = {"water": WATER, "vegetation": VEGETATION, "built_up": BUILT, "bare_soil": BARE}


def main() -> None:
    labels = base_scene()
    stack, _ = optical_stack(labels, seed=5)
    img = prepared_from_array(stack.astype(np.float32), "multispectral", roles=S2_ROLES,
                              name="areaA_t1", pixel_size=[PIXEL_M, PIXEL_M], crs=CRS_A)
    idx = compute_indices(img)

    print("=== per-class index means (truth-masked) ===")
    names = [n for n in ("NDVI", "NDWI", "MNDWI", "NDBI", "BRIGHTNESS", "TEXTURE")
             if idx.get(n) is not None]
    header = "class        " + "".join(f"{n:>12s}" for n in names)
    print(header)
    for cls, cid in TRUTH_ID.items():
        m = labels == cid
        row = f"{cls:12s}"
        for n in names:
            a = idx.get(n)[m]
            row += f"{a.mean():>7.3f}±{a.std():<4.2f}"
        print(row)

    result = measure_land_cover(img)
    print("\n=== thresholds applied ===")
    for name, t in result.thresholds.items():
        print(f"  {name:12s} value={t.value:+8.3f}  method={t.method:32s} "
              f"eta={t.separability:.3f}")
        if t.note:
            print(f"               note: {t.note}")

    print("\n=== truth vs measured ===")
    print(f"{'class':14s}{'truth':>9s}{'measured':>10s}{'recall':>9s}{'precision':>11s}")
    for cls in CLASS_ORDER:
        if cls == "other":
            continue
        truth = labels == TRUTH_ID[cls]
        pred = result.mask(cls)
        inter = float((truth & pred).sum())
        recall = inter / max(float(truth.sum()), 1.0)
        precision = inter / max(float(pred.sum()), 1.0)
        print(f"{cls:14s}{truth.mean():>9.4f}{pred.mean():>10.4f}"
              f"{recall:>9.2f}{precision:>11.2f}")
    other = result.mask("other")
    print(f"{'other':14s}{'-':>9s}{other.mean():>10.4f}")

    print("\n=== where did true built-up actually go? ===")
    built_truth = labels == BUILT
    for cls in CLASS_ORDER:
        share = float((built_truth & result.mask(cls)).sum()) / max(float(built_truth.sum()), 1.0)
        if share > 0.01:
            print(f"  {share * 100:5.1f}% of true built-up was labelled {cls}")


if __name__ == "__main__":
    main()
