"""Runtime configuration for the SatQuery AI backend.

Every knob is environment-driven so the same code runs on a laptop (analysis
engine only, no GPU), on a GPU box (analysis engine + Qwen3-VL narration), and
inside a Kaggle/Colab training run (LoRA adapters).
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # torch is optional: the analysis engine never needs it
    import torch

    _HAS_TORCH = True
except Exception:  # pragma: no cover - depends on the host environment
    _HAS_TORCH = False


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
RUNTIME_DIR = Path(os.environ.get("SATQUERY_RUNTIME", BASE_DIR / "runtime"))
UPLOAD_DIR = RUNTIME_DIR / "uploads"
OUTPUT_DIR = RUNTIME_DIR / "outputs"
TRACE_DIR = RUNTIME_DIR / "traces"
JOB_DIR = RUNTIME_DIR / "jobs"
EXAMPLE_DIR = RUNTIME_DIR / "examples"
MODEL_DIR = Path(os.environ.get("SATQUERY_MODEL_DIR", PROJECT_DIR / "models"))
DEMO_DIR = RUNTIME_DIR / "demo_data"

for _d in (UPLOAD_DIR, OUTPUT_DIR, TRACE_DIR, JOB_DIR, EXAMPLE_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def default_device() -> str:
    if not _HAS_TORCH:
        return "cpu"
    try:
        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:  # pragma: no cover
        pass
    return "cpu"


class Settings:
    """Central settings object; import the singleton ``settings``."""

    # --- Narration backend (optional; the analysis engine works without it) ---
    use_real_model: bool = _bool("USE_REAL_MODEL", False)
    use_mock: bool = _bool("SATQUERY_MOCK", False)
    model_id: str = os.environ.get("MODEL_ID", "Qwen/Qwen3-VL-4B-Instruct")
    adapter_rs_vlm: str = os.environ.get("ADAPTER_RS_VLM", "")
    adapter_change: str = os.environ.get("ADAPTER_CHANGE", "")
    adapter_fusion: str = os.environ.get("ADAPTER_FUSION", "")
    judge_model_id: str = os.environ.get("JUDGE_MODEL_ID", "Qwen/Qwen3-8B")
    device: str = os.environ.get("SATQUERY_DEVICE", default_device())
    quantize_bits: int = _int("SATQUERY_QUANT", 4)
    self_consistency_samples: int = _int("SATQUERY_SELF_CONSISTENCY", 1)

    # --- Adapted (BigEarthNet-MM) land-cover head ---
    ben_head_path: str = os.environ.get("BEN_HEAD_PATH", str(MODEL_DIR / "ben_mm_lc.npz"))
    use_ben_head: bool = _bool("SATQUERY_USE_BEN_HEAD", True)

    # --- Imagery handling ---
    tile_size: int = _int("SATQUERY_TILE_SIZE", 512)
    max_tiles: int = _int("SATQUERY_MAX_TILES", 6)
    max_images: int = _int("SATQUERY_MAX_IMAGES", 2)
    max_file_mb: int = _int("SATQUERY_MAX_FILE_MB", 512)
    max_analysis_pixels: int = _int("SATQUERY_MAX_ANALYSIS_PIXELS", 4_194_304)  # 2048x2048
    preview_max_side: int = _int("SATQUERY_PREVIEW_MAX_SIDE", 1024)

    # --- Analysis engine defaults (all exposed as permitted task parameters) ---
    min_region_pixels: int = _int("SATQUERY_MIN_REGION_PX", 64)
    smoothing_window: int = _int("SATQUERY_SMOOTH_WIN", 3)
    change_significance: float = _float("SATQUERY_CHANGE_SIGNIFICANCE", 0.01)
    speckle_filter: bool = _bool("SATQUERY_SPECKLE_FILTER", True)
    coregistration_min_overlap: float = _float("SATQUERY_MIN_OVERLAP", 0.5)

    # --- Generation ---
    max_new_tokens: int = _int("SATQUERY_MAX_NEW_TOKENS", 512)
    temperature: float = _float("SATQUERY_TEMPERATURE", 0.2)
    timeouts_s: int = _int("SATQUERY_TIMEOUT_S", 600)

    version: str = "0.2.0"
    has_torch: bool = _HAS_TORCH

    def to_audit(self) -> dict:
        """Environment block for the audit trace: what actually ran."""
        return {"server_version": self.version, "device": self.device,
                "torch_available": self.has_torch,
                "vlm_narration_enabled": self.use_real_model,
                "vlm_model_id": self.model_id if self.use_real_model else None,
                "mock_mode": self.use_mock,
                "max_analysis_pixels": self.max_analysis_pixels}


settings = Settings()
