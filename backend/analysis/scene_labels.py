"""Adapted BigEarthNet-MM scene-label head.

The problem statement requires at least one visual component **adapted on
remote-sensing data** (BigEarthNet is named explicitly). This module is the
inference side of that component: a small dual-branch (Sentinel-2 optical +
Sentinel-1 SAR) multi-label land-cover classifier trained by
``training/adapt_ben_mm.py`` on BigEarthNet-MM patches with the CORINE-derived
19-class nomenclature.

The feature extractor lives here rather than in the trainer on purpose -- train
and inference must compute byte-identical features, and the only way to guarantee
that is to share the code. The saved ``.npz`` records the feature names it was
trained on and inference refuses to run if they disagree, so a stale model can
never silently score garbage.

When no trained head is present the module returns nothing at all. It does not
invent labels: an absent adapted model is reported as absent.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from backend.config import settings

# --- Feature specification (shared with the trainer) -------------------------

_INDEX_FEATURES = ("NDVI", "NDWI", "MNDWI", "NDBI", "BRIGHTNESS", "TEXTURE")
_BAND_FEATURES = ("blue", "green", "red", "nir", "swir1", "swir2")
_SAR_FEATURES = ("SAR_DB", "SAR_TEXTURE", "SAR_RATIO")
_STATS = ("mean", "std", "p10", "p50", "p90")


def feature_names() -> List[str]:
    names: List[str] = []
    for idx in _INDEX_FEATURES:
        names += [f"opt.{idx}.{s}" for s in _STATS]
        names.append(f"opt.{idx}.present")
    for band in _BAND_FEATURES:
        names += [f"band.{band}.mean", f"band.{band}.std", f"band.{band}.present"]
    for idx in _SAR_FEATURES:
        names += [f"sar.{idx}.{s}" for s in _STATS]
        names.append(f"sar.{idx}.present")
    return names


FEATURE_NAMES = feature_names()
FEATURE_DIM = len(FEATURE_NAMES)


def _stats_of(arr: Optional[np.ndarray], valid: Optional[np.ndarray]) -> Tuple[List[float], float]:
    if arr is None:
        return [0.0] * len(_STATS), 0.0
    pool = arr[valid] if valid is not None and valid.shape == arr.shape else arr
    pool = np.asarray(pool, dtype=np.float64).ravel()
    pool = pool[np.isfinite(pool)]
    if pool.size == 0:
        return [0.0] * len(_STATS), 0.0
    return ([float(pool.mean()), float(pool.std()),
             float(np.percentile(pool, 10)), float(np.percentile(pool, 50)),
             float(np.percentile(pool, 90))], 1.0)


def _scaled_band(img, role: str) -> Optional[np.ndarray]:
    """Band values scaled to a sensor-independent 0..1 range."""
    b = img.band(role)
    if b is None:
        return None
    from backend.analysis.thresholds import robust_scale

    return robust_scale(b, img.valid)


def extract_features(optical=None, sar=None, optical_stack=None, sar_stack=None) -> np.ndarray:
    """Build the fixed-length dual-branch feature vector.

    Either branch may be missing (single-image inference, or a patch whose S1
    counterpart failed to download); the corresponding ``present`` flags go to
    zero and the trained head has seen that pattern during training.
    """
    from backend.analysis.indices import compute_indices

    if optical is not None and optical_stack is None:
        optical_stack = compute_indices(optical)
    if sar is not None and sar_stack is None:
        sar_stack = compute_indices(sar)

    vec: List[float] = []
    ovalid = optical.valid if optical is not None else None
    for idx in _INDEX_FEATURES:
        arr = optical_stack.get(idx) if optical_stack is not None else None
        stats, present = _stats_of(arr, ovalid)
        vec += stats + [present]
    for band in _BAND_FEATURES:
        arr = _scaled_band(optical, band) if optical is not None else None
        if arr is None:
            vec += [0.0, 0.0, 0.0]
        else:
            pool = arr[ovalid] if ovalid is not None else arr
            pool = pool[np.isfinite(pool)]
            vec += ([float(pool.mean()), float(pool.std()), 1.0] if pool.size else [0.0, 0.0, 0.0])
    svalid = sar.valid if sar is not None else None
    for idx in _SAR_FEATURES:
        arr = sar_stack.get(idx) if sar_stack is not None else None
        stats, present = _stats_of(arr, svalid)
        vec += stats + [present]

    out = np.asarray(vec, dtype=np.float32)
    if out.size != FEATURE_DIM:  # pragma: no cover - guards a spec/code mismatch
        raise RuntimeError(f"feature vector is {out.size} long, spec says {FEATURE_DIM}")
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)


# --- Model loading and inference --------------------------------------------

_lock = threading.Lock()
_cache: Dict[str, object] = {}


class SceneLabelHead:
    """Two-layer multi-label MLP with the standardisation it was trained under."""

    def __init__(self, payload: dict):
        self.w1 = np.asarray(payload["w1"], dtype=np.float32)
        self.b1 = np.asarray(payload["b1"], dtype=np.float32)
        self.w2 = np.asarray(payload["w2"], dtype=np.float32)
        self.b2 = np.asarray(payload["b2"], dtype=np.float32)
        self.mean = np.asarray(payload["feature_mean"], dtype=np.float32)
        self.std = np.asarray(payload["feature_std"], dtype=np.float32)
        self.classes: List[str] = [str(c) for c in payload["classes"]]
        self.names: List[str] = [str(c) for c in payload["feature_names"]]
        self.thresholds = np.asarray(payload.get("thresholds", np.full(len(self.classes), 0.5)),
                                     dtype=np.float32)
        meta = payload.get("meta")
        self.meta: dict = {}
        if meta is not None:
            try:
                import json

                self.meta = json.loads(str(np.asarray(meta).item()))
            except Exception:
                self.meta = {}
        self.model_id = str(self.meta.get("model_id", "ben_mm_lc"))

    def predict(self, features: np.ndarray) -> np.ndarray:
        x = (features - self.mean) / np.maximum(self.std, 1e-6)
        h = np.maximum(x @ self.w1 + self.b1, 0.0)
        z = h @ self.w2 + self.b2
        return 1.0 / (1.0 + np.exp(-z))


def load_head(path: Optional[str] = None) -> Optional[SceneLabelHead]:
    """Load the adapted head, or return None when it has not been trained yet."""
    if not settings.use_ben_head:
        return None
    p = Path(path or settings.ben_head_path)
    key = str(p)
    with _lock:
        if key in _cache:
            head = _cache[key]
            return head if isinstance(head, SceneLabelHead) else None
        if not p.exists():
            _cache[key] = "missing"
            return None
        try:
            with np.load(p, allow_pickle=False) as z:
                payload = {k: z[k] for k in z.files}
            payload["classes"] = [str(c) for c in payload["classes"]]
            payload["feature_names"] = [str(c) for c in payload["feature_names"]]
            head = SceneLabelHead(payload)
            if head.names != FEATURE_NAMES:
                print(f"[scene_labels] refusing {p.name}: trained on a different feature spec "
                      f"({len(head.names)} features vs {FEATURE_DIM}); retrain with the current code")
                _cache[key] = "mismatch"
                return None
            _cache[key] = head
            return head
        except Exception as exc:  # pragma: no cover - corrupt artefact
            print(f"[scene_labels] failed to load {p}: {exc}")
            _cache[key] = "error"
            return None


def describe() -> dict:
    """Registry-facing description of the adapted component."""
    head = load_head()
    if head is None:
        return {"available": False, "path": settings.ben_head_path,
                "note": "adapted BigEarthNet-MM head not trained yet; "
                        "run training/adapt_ben_mm.py to produce it"}
    return {"available": True, "model_id": head.model_id, "classes": len(head.classes),
            "features": len(head.names), "training": head.meta}


def predict(optical=None, optical_stack=None, sar=None, sar_stack=None,
            top_k: int = 6) -> Tuple[List[dict], Optional[str]]:
    """Multi-label scene predictions from the adapted head.

    Returns ``([], None)`` when no adapted head exists -- callers must not
    fabricate labels in that case.
    """
    head = load_head()
    if head is None:
        return [], None
    try:
        feats = extract_features(optical=optical, optical_stack=optical_stack,
                                 sar=sar, sar_stack=sar_stack)
    except Exception as exc:  # pragma: no cover
        print(f"[scene_labels] feature extraction failed: {exc}")
        return [], None
    probs = head.predict(feats)
    order = np.argsort(probs)[::-1]
    out: List[dict] = []
    for i in order[: max(1, top_k)]:
        out.append({"label": head.classes[i], "probability": round(float(probs[i]), 4),
                    "above_threshold": bool(probs[i] >= head.thresholds[i])})
    modality = "optical+SAR" if (optical is not None and sar is not None) else (
        "SAR-only" if sar is not None else "optical-only")
    return out, f"{head.model_id} (adapted on BigEarthNet-MM, {modality} branch active)"


def positive_labels(preds: Sequence[dict]) -> List[str]:
    return [p["label"] for p in preds if p.get("above_threshold")]


def label_margin(preds: Sequence[dict]) -> Optional[float]:
    """Top-1 minus top-2 probability: a real (learned) confidence signal."""
    if len(preds) < 2:
        return None
    return round(float(preds[0]["probability"] - preds[1]["probability"]), 4)
