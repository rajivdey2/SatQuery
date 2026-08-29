"""Runtime configuration for the SatQuery AI backend.

All knobs are env-driven so the same code runs on a laptop (mock), a GPU box
(zero-shot Qwen3-VL), and a Kaggle/Colab training run (LoRA adapters).
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # torch is optional at runtime
    import torch

    _HAS_TORCH = True
except Exception:  # pragma: no cover
    _HAS_TORCH = False


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path(os.environ.get("SATQUERY_RUNTIME", BASE_DIR / "runtime"))
UPLOAD_DIR = RUNTIME_DIR / "uploads"
OUTPUT_DIR = RUNTIME_DIR / "outputs"
TRACE_DIR = RUNTIME_DIR / "traces"

for _d in (UPLOAD_DIR, OUTPUT_DIR, TRACE_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def default_device() -> str:
    if _HAS_TORCH and torch.cuda.is_available():
        return "cuda"
    if _HAS_TORCH and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class Settings:
    """Central settings object; import the singleton `settings`."""

    use_real_model: bool = _bool("USE_REAL_MODEL", False)
    model_id: str = os.environ.get("MODEL_ID", "Qwen/Qwen3-VL-4B-Instruct")
    adapter_rs_vlm: str = os.environ.get("ADAPTER_RS_VLM", "")
    adapter_change: str = os.environ.get("ADAPTER_CHANGE", "")
    adapter_fusion: str = os.environ.get("ADAPTER_FUSION", "")
    judge_model_id: str = os.environ.get("JUDGE_MODEL_ID", "Qwen/Qwen3-8B")
    device: str = os.environ.get("SATQUERY_DEVICE", default_device())
    quantize_bits: int = int(os.environ.get("SATQUERY_QUANT", "4"))
    tile_size: int = int(os.environ.get("SATQUERY_TILE_SIZE", "512"))
    max_tiles: int = int(os.environ.get("SATQUERY_MAX_TILES", "6"))
    max_images: int = int(os.environ.get("SATQUERY_MAX_IMAGES", "4"))
    max_file_mb: int = int(os.environ.get("SATQUERY_MAX_FILE_MB", "512"))
    max_new_tokens: int = int(os.environ.get("SATQUERY_MAX_NEW_TOKENS", "512"))
    temperature: float = float(os.environ.get("SATQUERY_TEMPERATURE", "0.2"))
    timeouts_s: int = int(os.environ.get("SATQUERY_TIMEOUT_S", "600"))
    version: str = "0.1.0"
    has_torch: bool = _HAS_TORCH


settings = Settings()