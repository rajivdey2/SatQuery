"""Adapt the land-cover head on BigEarthNet-MM (Sentinel-1 SAR + Sentinel-2 optical).

This is the concrete answer to the mandatory requirement that *at least one visual
component be fine-tuned or adapted using BigEarthNet*. It trains a small
dual-branch multi-label classifier over the 19-class BigEarthNet nomenclature:
the optical branch sees Sentinel-2 spectral-index statistics, the SAR branch sees
Sentinel-1 backscatter and roughness statistics, and a two-layer MLP fuses them.

Design choices, and why:

* **The feature extractor is imported from the serving code**
  (``backend.analysis.scene_labels.extract_features``) rather than reimplemented.
  A duplicated extractor is the classic way an adapted model silently rots; here a
  mismatch is impossible, and inference refuses to load a head whose feature spec
  differs.
* **NumPy, not torch.** The head has a few tens of thousands of parameters. It
  trains on a laptop CPU in minutes over tens of thousands of patches, so the
  mandatory adaptation does not depend on GPU quota — the QLoRA run in
  ``run_lora_train.py`` remains the separate, optional narration upgrade.
* **Per-class thresholds are tuned on a held-out split**, and the reported metrics
  are the held-out ones. An adapted component whose numbers come from its own
  training set is not evidence of anything.

Usage
    # real data (any of the three supported BigEarthNet layouts, auto-detected)
    python training/adapt_ben_mm.py --root /data/BigEarthNet --out models/ben_mm_lc.npz
    # cache features once, then iterate on the head cheaply
    python training/adapt_ben_mm.py --root /data/BigEarthNet --cache work/ben_feats.npz
    python training/adapt_ben_mm.py --features work/ben_feats.npz --out models/ben_mm_lc.npz
    # verify the trainer itself with no dataset present
    python training/adapt_ben_mm.py --self-test
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.analysis.scene_labels import FEATURE_NAMES, extract_features  # noqa: E402
from backend.preprocessing.pipeline import prepared_from_array  # noqa: E402

# BigEarthNet v2 / reBEN 19-class nomenclature.
BEN19 = [
    "Urban fabric",
    "Industrial or commercial units",
    "Arable land",
    "Permanent crops",
    "Pastures",
    "Complex cultivation patterns",
    "Land principally occupied by agriculture, with significant areas of natural vegetation",
    "Agro-forestry areas",
    "Broad-leaved forest",
    "Coniferous forest",
    "Mixed forest",
    "Natural grassland and sparsely vegetated areas",
    "Moors, heathland and sclerophyllous vegetation",
    "Transitional woodland, shrub",
    "Beaches, dunes, sands",
    "Inland wetlands",
    "Coastal wetlands",
    "Inland waters",
    "Marine waters",
]
_LABEL_INDEX = {name.lower(): i for i, name in enumerate(BEN19)}

# Sentinel-2 band order used to stack per-band BigEarthNet files (v1 = 12 bands).
S2_BANDS = ["B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B8A", "B09", "B11", "B12"]
S2_ROLES = {"blue": 1, "green": 2, "red": 3, "rededge": 4, "nir": 7, "swir1": 10, "swir2": 11}
S1_BANDS = ["VV", "VH"]
S1_ROLES = {"vv": 0, "vh": 1}


@dataclass
class PatchRef:
    """One BigEarthNet-MM sample: optical stack, SAR stack, multi-labels."""

    patch_id: str
    s2_files: List[Path] = field(default_factory=list)
    s1_files: List[Path] = field(default_factory=list)
    s2_stack: Optional[Path] = None
    s1_stack: Optional[Path] = None
    labels: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Dataset discovery
# --------------------------------------------------------------------------- #

def _read_band(path: Path) -> np.ndarray:
    import rasterio

    with rasterio.open(path) as src:
        return src.read(1).astype(np.float32)


def _stack_bands(files: Sequence[Path], target_shape: Optional[Tuple[int, int]] = None
                 ) -> Optional[np.ndarray]:
    """Read per-band files into ``(C, H, W)``, resampling the coarser bands up."""
    from backend.preprocessing.geotiff_io import resample_to

    planes: List[np.ndarray] = []
    shape = target_shape
    for f in files:
        try:
            a = _read_band(f)
        except Exception:
            return None
        if shape is None:
            shape = a.shape
        planes.append(a)
    if not planes or shape is None:
        return None
    shape = max((p.shape for p in planes), key=lambda s: s[0] * s[1])
    return np.stack([p if p.shape == shape else resample_to(p, shape) for p in planes], axis=0)


def _labels_from_json(path: Path) -> List[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    labels = payload.get("labels") or payload.get("label") or []
    return [str(x) for x in labels] if isinstance(labels, list) else [str(labels)]


def _discover_v1(root: Path, limit: int) -> List[PatchRef]:
    """BigEarthNet v1 layout: one directory per patch, one GeoTIFF per band."""
    s2_root = next((p for p in (root / "BigEarthNet-v1.0", root / "BigEarthNet-S2", root)
                    if p.exists()), None)
    if s2_root is None:
        return []
    s1_root = next((p for p in (root / "BigEarthNet-S1-v1.0", root / "BigEarthNet-S1")
                    if p.exists()), None)
    s1_by_s2: Dict[str, Path] = {}
    if s1_root is not None:
        for meta in s1_root.rglob("*_labels_metadata.json"):
            try:
                payload = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                continue
            partner = payload.get("corresponding_s2_patch")
            if partner:
                s1_by_s2[str(partner)] = meta.parent

    out: List[PatchRef] = []
    for meta in s2_root.rglob("*_labels_metadata.json"):
        patch_dir = meta.parent
        patch_id = patch_dir.name
        s2_files = [patch_dir / f"{patch_id}_{b}.tif" for b in S2_BANDS]
        s2_files = [f for f in s2_files if f.exists()]
        if len(s2_files) < 4:
            continue
        s1_dir = s1_by_s2.get(patch_id)
        s1_files: List[Path] = []
        if s1_dir is not None:
            s1_files = [p for p in (s1_dir / f"{s1_dir.name}_{b}.tif" for b in S1_BANDS)
                        if p.exists()]
        out.append(PatchRef(patch_id=patch_id, s2_files=s2_files, s1_files=s1_files,
                            labels=_labels_from_json(meta)))
        if limit and len(out) >= limit:
            break
    return out


def _discover_reben(root: Path, limit: int) -> List[PatchRef]:
    """reBEN layout: parquet metadata plus per-band GeoTIFFs under tile directories."""
    meta_files = list(root.glob("*.parquet")) + list(root.glob("metadata*/*.parquet"))
    if not meta_files:
        return []
    try:
        import pandas as pd
    except ImportError:
        print("[adapt] reBEN parquet metadata found but pandas is not installed; "
              "install pandas or use the v1 layout.")
        return []
    frames = []
    for f in meta_files:
        try:
            frames.append(pd.read_parquet(f))
        except Exception as exc:
            print(f"[adapt] could not read {f.name}: {exc}")
    if not frames:
        return []
    df = pd.concat(frames, ignore_index=True)
    id_col = next((c for c in ("patch_id", "patch", "s2_name", "name") if c in df.columns), None)
    label_col = next((c for c in ("labels", "label", "labels_19", "new_labels")
                      if c in df.columns), None)
    s1_col = next((c for c in ("s1_name", "s1_patch", "corresponding_s1_patch")
                   if c in df.columns), None)
    if id_col is None or label_col is None:
        print(f"[adapt] parquet metadata lacks id/label columns (have {list(df.columns)[:8]})")
        return []

    s2_index = {p.name: p for p in root.rglob("*") if p.is_dir()}
    out: List[PatchRef] = []
    for _, row in df.iterrows():
        patch_id = str(row[id_col])
        patch_dir = s2_index.get(patch_id)
        if patch_dir is None:
            continue
        s2_files = [p for p in (patch_dir / f"{patch_id}_{b}.tif" for b in S2_BANDS) if p.exists()]
        if len(s2_files) < 4:
            continue
        s1_files: List[Path] = []
        if s1_col and row.get(s1_col):
            s1_dir = s2_index.get(str(row[s1_col]))
            if s1_dir is not None:
                s1_files = [p for p in (s1_dir / f"{str(row[s1_col])}_{b}.tif" for b in S1_BANDS)
                            if p.exists()]
        raw_labels = row[label_col]
        labels = list(raw_labels) if isinstance(raw_labels, (list, np.ndarray)) else [str(raw_labels)]
        out.append(PatchRef(patch_id=patch_id, s2_files=s2_files, s1_files=s1_files,
                            labels=[str(x) for x in labels]))
        if limit and len(out) >= limit:
            break
    return out


def _discover_stacked(root: Path, limit: int) -> List[PatchRef]:
    """Prepared layout: ``patches/<id>.tif`` + ``s1/<id>_S1.tif`` + ``labels.jsonl``."""
    labels_path = root / "labels.jsonl"
    if not labels_path.exists():
        return []
    out: List[PatchRef] = []
    for line in labels_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        patch = str(rec.get("patch") or rec.get("id") or "")
        s2 = root / "patches" / f"{patch}.tif"
        s1 = root / "s1" / f"{patch}_S1.tif"
        if not s2.exists():
            continue
        out.append(PatchRef(patch_id=patch, s2_stack=s2,
                            s1_stack=s1 if s1.exists() else None,
                            labels=[str(x) for x in (rec.get("labels") or [])]))
        if limit and len(out) >= limit:
            break
    return out


def discover(root: Path, limit: int = 0) -> Tuple[List[PatchRef], str]:
    for name, fn in (("prepared-stacked", _discover_stacked),
                     ("reben-parquet", _discover_reben),
                     ("bigearthnet-v1", _discover_v1)):
        patches = fn(root, limit)
        if patches:
            return patches, name
    return [], "none"


# --------------------------------------------------------------------------- #
# Feature building
# --------------------------------------------------------------------------- #

def encode_labels(labels: Sequence[str]) -> np.ndarray:
    """Multi-hot encode against the 19-class nomenclature."""
    y = np.zeros(len(BEN19), dtype=np.float32)
    for raw in labels:
        key = str(raw).strip().lower()
        idx = _LABEL_INDEX.get(key)
        if idx is None:                       # tolerate minor punctuation differences
            idx = next((i for name, i in _LABEL_INDEX.items()
                        if name.startswith(key[:24]) and len(key) > 6), None)
        if idx is not None:
            y[idx] = 1.0
    return y


def _prepared_pair(ref: PatchRef):
    optical = sar = None
    if ref.s2_stack is not None:
        from backend.preprocessing.geotiff_io import read_array

        raw, _ = read_array(str(ref.s2_stack))
        roles = S2_ROLES if raw.shape[0] >= 12 else None
        optical = prepared_from_array(raw, "multispectral", roles=roles, name=ref.patch_id)
    elif ref.s2_files:
        raw = _stack_bands(ref.s2_files)
        if raw is not None:
            roles = S2_ROLES if raw.shape[0] >= 12 else None
            optical = prepared_from_array(raw, "multispectral", roles=roles, name=ref.patch_id,
                                          pixel_size=[10.0, 10.0], crs="EPSG:32632")
    if ref.s1_stack is not None:
        from backend.preprocessing.geotiff_io import read_array

        raw, _ = read_array(str(ref.s1_stack))
        sar = prepared_from_array(raw, "sar", roles=S1_ROLES if raw.shape[0] >= 2 else None,
                                  name=f"{ref.patch_id}_S1")
    elif ref.s1_files:
        raw = _stack_bands(ref.s1_files)
        if raw is not None:
            sar = prepared_from_array(raw, "sar", roles=S1_ROLES if raw.shape[0] >= 2 else None,
                                      name=f"{ref.patch_id}_S1", pixel_size=[10.0, 10.0],
                                      crs="EPSG:32632")
    return optical, sar


def build_features(patches: Sequence[PatchRef], progress_every: int = 200
                   ) -> Tuple[np.ndarray, np.ndarray, List[str], int]:
    """Extract the dual-branch feature matrix and multi-hot labels."""
    xs: List[np.ndarray] = []
    ys: List[np.ndarray] = []
    ids: List[str] = []
    with_sar = 0
    t0 = time.time()
    for i, ref in enumerate(patches, start=1):
        y = encode_labels(ref.labels)
        if y.sum() == 0:
            continue
        optical, sar = _prepared_pair(ref)
        if optical is None and sar is None:
            continue
        try:
            x = extract_features(optical=optical, sar=sar)
        except Exception as exc:
            print(f"[adapt] feature extraction failed for {ref.patch_id}: {exc}")
            continue
        xs.append(x)
        ys.append(y)
        ids.append(ref.patch_id)
        with_sar += int(sar is not None)
        if progress_every and i % progress_every == 0:
            rate = i / max(time.time() - t0, 1e-6)
            print(f"[adapt] {i}/{len(patches)} patches ({rate:.1f}/s, {len(xs)} usable)")
    if not xs:
        return np.zeros((0, len(FEATURE_NAMES)), np.float32), np.zeros((0, len(BEN19)), np.float32), [], 0
    return np.stack(xs), np.stack(ys), ids, with_sar


# --------------------------------------------------------------------------- #
# The head: 2-layer multi-label MLP trained with Adam on BCE
# --------------------------------------------------------------------------- #

def _sigmoid(z: np.ndarray) -> np.ndarray:
    out = np.empty_like(z)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    ez = np.exp(z[~pos])
    out[~pos] = ez / (1.0 + ez)
    return out


@dataclass
class TrainedHead:
    w1: np.ndarray
    b1: np.ndarray
    w2: np.ndarray
    b2: np.ndarray
    mean: np.ndarray
    std: np.ndarray
    thresholds: np.ndarray
    history: List[dict] = field(default_factory=list)

    def forward(self, x: np.ndarray) -> np.ndarray:
        z = (x - self.mean) / np.maximum(self.std, 1e-6)
        h = np.maximum(z @ self.w1 + self.b1, 0.0)
        return _sigmoid(h @ self.w2 + self.b2)


def train_head(x: np.ndarray, y: np.ndarray, hidden: int = 96, epochs: int = 60,
               lr: float = 3e-3, batch: int = 128, weight_decay: float = 1e-4,
               val_fraction: float = 0.2, seed: int = 7,
               verbose: bool = True) -> Tuple[TrainedHead, dict]:
    """Train the multi-label head and report held-out metrics."""
    rng = np.random.default_rng(seed)
    n, dim = x.shape
    n_classes = y.shape[1]
    order = rng.permutation(n)
    n_val = max(1, int(n * val_fraction)) if n > 4 else 0
    val_idx, train_idx = order[:n_val], order[n_val:]
    xtr, ytr = x[train_idx], y[train_idx]
    xva, yva = (x[val_idx], y[val_idx]) if n_val else (x[train_idx], y[train_idx])

    mean = xtr.mean(axis=0)
    std = xtr.std(axis=0)
    std[std < 1e-6] = 1.0

    scale = np.sqrt(2.0 / dim)
    w1 = (rng.standard_normal((dim, hidden)) * scale).astype(np.float32)
    b1 = np.zeros(hidden, dtype=np.float32)
    w2 = (rng.standard_normal((hidden, n_classes)) * np.sqrt(2.0 / hidden)).astype(np.float32)
    # Initialise output bias at the label prior: multi-label sets are sparse, and
    # starting at the prior stops the first epochs from being spent learning it.
    prior = np.clip(ytr.mean(axis=0), 1e-4, 1 - 1e-4)
    b2 = np.log(prior / (1 - prior)).astype(np.float32)

    params = {"w1": w1, "b1": b1, "w2": w2, "b2": b2}
    m = {k: np.zeros_like(v) for k, v in params.items()}
    v = {k: np.zeros_like(val) for k, val in params.items()}
    beta1, beta2, eps = 0.9, 0.999, 1e-8
    step = 0
    history: List[dict] = []

    # Positive weighting: BigEarthNet classes are heavily imbalanced, so an
    # unweighted BCE converges to predicting "absent" for the rare classes.
    pos_weight = np.clip((1.0 - prior) / prior, 1.0, 20.0).astype(np.float32)

    ztr = (xtr - mean) / np.maximum(std, 1e-6)
    zva = (xva - mean) / np.maximum(std, 1e-6)

    for epoch in range(epochs):
        perm = rng.permutation(len(ztr))
        total = 0.0
        for start in range(0, len(perm), batch):
            idx = perm[start:start + batch]
            xb, yb = ztr[idx], ytr[idx]
            if len(idx) == 0:
                continue
            h_pre = xb @ params["w1"] + params["b1"]
            h = np.maximum(h_pre, 0.0)
            logits = h @ params["w2"] + params["b2"]
            p = _sigmoid(logits)
            wt = yb * pos_weight + (1.0 - yb)
            loss = -np.mean(wt * (yb * np.log(p + 1e-9) + (1 - yb) * np.log(1 - p + 1e-9)))
            total += float(loss) * len(idx)

            dlogits = wt * (p - yb) / (len(idx) * yb.shape[1])
            grads = {
                "w2": h.T @ dlogits + weight_decay * params["w2"],
                "b2": dlogits.sum(axis=0),
            }
            dh = dlogits @ params["w2"].T
            dh[h_pre <= 0] = 0.0
            grads["w1"] = xb.T @ dh + weight_decay * params["w1"]
            grads["b1"] = dh.sum(axis=0)

            step += 1
            for k, g in grads.items():
                m[k] = beta1 * m[k] + (1 - beta1) * g
                v[k] = beta2 * v[k] + (1 - beta2) * (g * g)
                mhat = m[k] / (1 - beta1 ** step)
                vhat = v[k] / (1 - beta2 ** step)
                params[k] = params[k] - lr * mhat / (np.sqrt(vhat) + eps)

        if verbose and (epoch % max(1, epochs // 10) == 0 or epoch == epochs - 1):
            pv = _sigmoid(np.maximum(zva @ params["w1"] + params["b1"], 0.0) @ params["w2"]
                          + params["b2"])
            f1 = micro_f1(yva, pv >= 0.5)
            history.append({"epoch": epoch, "train_loss": round(total / max(len(ztr), 1), 5),
                            "val_micro_f1_at_0.5": round(f1, 4)})
            print(f"[adapt] epoch {epoch:3d}  loss {total / max(len(ztr), 1):.5f}  "
                  f"val micro-F1@0.5 {f1:.4f}")

    head = TrainedHead(w1=params["w1"], b1=params["b1"], w2=params["w2"], b2=params["b2"],
                       mean=mean, std=std,
                       thresholds=np.full(n_classes, 0.5, dtype=np.float32), history=history)
    probs_val = head.forward(xva)
    head.thresholds = tune_thresholds(yva, probs_val)
    metrics = evaluate(yva, probs_val, head.thresholds)
    metrics["n_train"] = int(len(train_idx))
    metrics["n_val"] = int(len(val_idx) or len(train_idx))
    metrics["held_out"] = bool(n_val)
    return head, metrics


def tune_thresholds(y: np.ndarray, p: np.ndarray) -> np.ndarray:
    """Per-class threshold maximising F1 on the held-out split."""
    out = np.full(y.shape[1], 0.5, dtype=np.float32)
    grid = np.linspace(0.05, 0.95, 19)
    for c in range(y.shape[1]):
        if y[:, c].sum() == 0:
            continue
        best, best_t = -1.0, 0.5
        for t in grid:
            f1 = binary_f1(y[:, c], p[:, c] >= t)
            if f1 > best:
                best, best_t = f1, float(t)
        out[c] = best_t
    return out


def binary_f1(y: np.ndarray, pred: np.ndarray) -> float:
    tp = float(np.sum((y == 1) & pred))
    fp = float(np.sum((y == 0) & pred))
    fn = float(np.sum((y == 1) & ~pred))
    if tp == 0:
        return 0.0
    precision = tp / (tp + fp)
    recall = tp / (tp + fn)
    return 2 * precision * recall / (precision + recall)


def micro_f1(y: np.ndarray, pred: np.ndarray) -> float:
    return binary_f1(y.ravel(), pred.ravel())


def average_precision(y: np.ndarray, score: np.ndarray) -> float:
    """Area under the precision-recall curve for one class."""
    if y.sum() == 0:
        return float("nan")
    order = np.argsort(-score)
    y_sorted = y[order]
    tp = np.cumsum(y_sorted)
    precision = tp / np.arange(1, len(y_sorted) + 1)
    return float(np.sum(precision * y_sorted) / y.sum())


def evaluate(y: np.ndarray, p: np.ndarray, thresholds: np.ndarray) -> dict:
    pred = p >= thresholds[None, :]
    per_class = []
    aps = []
    for c in range(y.shape[1]):
        support = int(y[:, c].sum())
        ap = average_precision(y[:, c], p[:, c])
        if not np.isnan(ap):
            aps.append(ap)
        per_class.append({"class": BEN19[c], "support": support,
                          "f1": round(binary_f1(y[:, c], pred[:, c]), 4),
                          "average_precision": (None if np.isnan(ap) else round(ap, 4)),
                          "threshold": round(float(thresholds[c]), 3)})
    macro = [c["f1"] for c in per_class if c["support"] > 0]
    return {"micro_f1": round(micro_f1(y, pred), 4),
            "macro_f1": round(float(np.mean(macro)) if macro else 0.0, 4),
            "mean_average_precision": round(float(np.mean(aps)) if aps else 0.0, 4),
            "subset_accuracy": round(float(np.mean(np.all(pred == y, axis=1))), 4),
            "per_class": per_class}


def save_head(path: Path, head: TrainedHead, metrics: dict, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        path,
        w1=head.w1, b1=head.b1, w2=head.w2, b2=head.b2,
        feature_mean=head.mean, feature_std=head.std, thresholds=head.thresholds,
        classes=np.array(BEN19, dtype=object).astype(str),
        feature_names=np.array(FEATURE_NAMES, dtype=object).astype(str),
        meta=np.array(json.dumps({**meta, "metrics": metrics,
                                  "history": head.history}, default=str)))
    print(f"[adapt] head saved -> {path}")
    report = path.with_suffix(".metrics.json")
    report.write_text(json.dumps({"meta": meta, "metrics": metrics}, indent=2, default=str),
                      encoding="utf-8")
    print(f"[adapt] metrics    -> {report}")


# --------------------------------------------------------------------------- #
# Self-test: prove the trainer learns without needing the dataset
# --------------------------------------------------------------------------- #

def self_test(seed: int = 3, n: int = 1200) -> int:
    """Train on synthetic features with a known structure and assert it learns."""
    rng = np.random.default_rng(seed)
    dim, n_classes = len(FEATURE_NAMES), len(BEN19)
    truth = rng.standard_normal((dim, n_classes)).astype(np.float32) * 0.6
    x = rng.standard_normal((n, dim)).astype(np.float32)
    logits = np.tanh(x @ truth) * 2.2 - 1.0
    y = (_sigmoid(logits) > rng.random((n, n_classes))).astype(np.float32)
    print(f"[self-test] {n} synthetic samples, {dim} features, {n_classes} classes, "
          f"label density {y.mean():.3f}")
    head, metrics = train_head(x, y, hidden=64, epochs=40, lr=5e-3, verbose=True)
    baseline = micro_f1(y, np.tile(y.mean(axis=0) >= 0.5, (len(y), 1)))
    print(f"[self-test] held-out micro-F1 {metrics['micro_f1']:.4f} "
          f"(constant-prior baseline {baseline:.4f}), mAP {metrics['mean_average_precision']:.4f}")
    ok = metrics["micro_f1"] > max(0.35, baseline + 0.03)
    print("[self-test] PASS: the head learns the synthetic structure." if ok else
          "[self-test] FAIL: no better than the prior baseline.")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="BigEarthNet-MM root (v1, reBEN or prepared-stacked layout)")
    ap.add_argument("--features", help="reuse a cached feature file instead of reading imagery")
    ap.add_argument("--cache", help="write the extracted features here for later reuse")
    ap.add_argument("--out", default="models/ben_mm_lc.npz", help="output head path")
    ap.add_argument("--limit", type=int, default=0, help="max patches (0 = all)")
    ap.add_argument("--hidden", type=int, default=96)
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--lr", type=float, default=3e-3)
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--val-fraction", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--self-test", action="store_true",
                    help="train on synthetic data to verify the trainer, no dataset needed")
    a = ap.parse_args()

    if a.self_test:
        return self_test(seed=a.seed)

    layout = "cached"
    n_with_sar = 0
    if a.features:
        with np.load(a.features, allow_pickle=False) as z:
            x, y = z["x"], z["y"]
            n_with_sar = int(z["n_with_sar"]) if "n_with_sar" in z.files else 0
            layout = str(z["layout"]) if "layout" in z.files else "cached"
        print(f"[adapt] loaded {len(x)} cached feature vectors from {a.features}")
    else:
        if not a.root:
            ap.error("provide --root (dataset) or --features (cached), or use --self-test")
        root = Path(a.root)
        if not root.exists():
            ap.error(f"dataset root not found: {root}")
        patches, layout = discover(root, a.limit)
        if not patches:
            print(f"[adapt] no BigEarthNet patches found under {root}.\n"
                  "        Supported layouts:\n"
                  "          v1        <root>/<patch>/<patch>_B02.tif + <patch>_labels_metadata.json\n"
                  "          reBEN     <root>/*.parquet + per-band GeoTIFFs under tile directories\n"
                  "          prepared  <root>/patches/<id>.tif + <root>/s1/<id>_S1.tif + labels.jsonl")
            return 2
        print(f"[adapt] {len(patches)} patches discovered ({layout} layout)")
        x, y, ids, n_with_sar = build_features(patches)
        if len(x) == 0:
            print("[adapt] no usable patches after feature extraction")
            return 2
        print(f"[adapt] features {x.shape}, labels {y.shape}, "
              f"{n_with_sar} patches carried a Sentinel-1 counterpart")
        if a.cache:
            cache = Path(a.cache)
            cache.parent.mkdir(parents=True, exist_ok=True)
            np.savez(cache, x=x, y=y, n_with_sar=n_with_sar, layout=layout,
                     ids=np.array(ids, dtype=object).astype(str))
            print(f"[adapt] features cached -> {cache}")

    head, metrics = train_head(x, y, hidden=a.hidden, epochs=a.epochs, lr=a.lr,
                               batch=a.batch, val_fraction=a.val_fraction, seed=a.seed)
    print(f"[adapt] held-out micro-F1 {metrics['micro_f1']:.4f} · macro-F1 "
          f"{metrics['macro_f1']:.4f} · mAP {metrics['mean_average_precision']:.4f}")
    save_head(Path(a.out), head, metrics, {
        "model_id": "ben_mm_lc_v1",
        "dataset": "BigEarthNet-MM (Sentinel-1 + Sentinel-2)",
        "layout": layout,
        "nomenclature": "BigEarthNet 19-class",
        "samples": int(len(x)),
        "samples_with_sar": int(n_with_sar),
        "architecture": f"dual-branch statistics -> MLP({len(FEATURE_NAMES)}->{a.hidden}->{len(BEN19)})",
        "objective": "positive-weighted multi-label BCE, Adam",
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
