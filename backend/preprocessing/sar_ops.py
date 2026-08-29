"""SAR preprocessing: speckle filtering, dB scaling, per-scene normalization.

Real RADAR products (e.g., RISAT) store amplitude or intensity with wildly
different dynamic ranges; this module also recognises products already in dB
(BigEarthNet-S1 convention) and does not re-log them. Per-scene percentile
normalization keeps the VLM input appearance consistent.
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage

from backend.preprocessing.geotiff_io import percentile_normalize


def _to_db_channel(x: np.ndarray) -> np.ndarray:
    """Convert one SAR band to dB, leaving already-dB bands untouched."""
    x = np.asarray(x, dtype=np.float64)
    finite = x[np.isfinite(x)]
    if finite.size == 0:
        return x
    amin, amax = float(np.min(finite)), float(np.max(finite))
    # Negative values (or a flat near-zero band) -> already dB / no information.
    if amin < 0.0 or (amax - amin) < 1e-9:
        return x
    # Intensity products typically have far larger magnitudes than amplitude.
    if amax > 1e4:
        return 10.0 * np.log10(np.clip(x, 1e-8, None))
    return 20.0 * np.log10(np.clip(x, 1e-8, None))


def lee_filter(a: np.ndarray, win: int = 3, rms: float = 0.25) -> np.ndarray:
    """Lee speckle filter (single window) over a 2-D channel."""
    a = a.astype(np.float64)
    mu = ndimage.uniform_filter(a, size=win, mode="nearest")
    sq = ndimage.uniform_filter(a * a, size=win, mode="nearest")
    var = np.maximum(sq - mu * mu, 0.0)
    enl = (win * win / 4.4) ** 2  # effective number of looks (approximation)
    var_noise = rms * mu
    k = var / np.maximum(var + var_noise, 1e-8)
    return mu + k * (a - mu)


def sar_to_display(sar: np.ndarray, speckle_filter: bool = True) -> np.ndarray:
    """Raw SAR product band(s) -> pseudo-RGB uint8 display image.

    Handles single-band RISAT-style amplitude/intensity and BigEarthNet-S1
    dual-pol (VV/VH) dB products. Multi-band products are fused by averaging
    per-channel dB, then Lee-filtered (optional) and per-scene normalized.
    """
    a = np.asarray(sar, dtype=np.float64)
    if a.ndim == 3:
        channels = [_to_db_channel(x) for x in a]
        a = np.mean(np.stack(channels, axis=0), axis=0)
    elif a.ndim == 4:  # (tiles, bands, h, w) defensive squeeze
        a = a.reshape(1, a.shape[1], a.shape[2], a.shape[3])[:, 0] if a.shape[0] == 1 else np.mean(a, axis=0)
        if a.ndim == 3:
            channels = [_to_db_channel(x) for x in a]
            a = np.mean(np.stack(channels, axis=0), axis=0)
    else:
        a = _to_db_channel(a)

    if a.ndim != 2:
        a = percentile_normalize(a)
    if speckle_filter and a.size > 9:
        a = lee_filter(a, win=3)
    norm = percentile_normalize(a, 2.0, 98.0)
    rgb = np.stack([norm] * 3, axis=-1)
    return (np.clip(rgb, 0.0, 1.0) * 255).astype(np.uint8)