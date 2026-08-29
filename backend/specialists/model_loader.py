"""Lazy Qwen3-VL model loader.

Only activates when USE_REAL_MODEL=1 and torch+transformers are installed.
Kept import-safe so the API serves mock specialists on machines without the GPU
stack.

Prompt format and processor follow the official Qwen3-VL cookbook
(github.com/QwenLM/Qwen3-VL). Adapters (PEFT LoRA) load after the base model
when ADAPTER_*_PATH is set.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Optional

from backend.config import settings

_lock = threading.Lock()
_cache: Dict[str, Any] = {}


def _torch_ok() -> bool:
    if not settings.has_torch:
        return False
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        return True
    except Exception:
        return False


def load_model() -> Optional[Dict[str, Any]]:
    """Return {'model','processor','device','quantization'} or None."""
    if not settings.use_real_model:
        return None
    if not _torch_ok():
        return None
    with _lock:
        if "model" in _cache:
            return _cache
        try:
            from transformers import AutoModelForVision2Seq, AutoProcessor
            import torch

            dev = settings.device if settings.device != "auto" else ("cuda" if torch.cuda.is_available() else "cpu")
            quant = None
            kwargs: Dict[str, Any] = {"torch_dtype": torch.float16 if dev == "cuda" else torch.float32}
            if settings.quantize_bits in (4, 8) and dev == "cuda":
                try:
                    from transformers import BitsAndBytesConfig

                    bnb = BitsAndBytesConfig(
                        load_in_4bit=settings.quantize_bits == 4,
                        load_in_8bit=settings.quantize_bits == 8,
                        bnb_4bit_compute_dtype=torch.float16,
                        bnb_4bit_quant_type="nf4" if settings.quantize_bits == 4 else None,
                    )
                    kwargs["quantization_config"] = bnb
                    quant = f"{settings.quantize_bits}bit-bnb"
                except Exception:
                    quant = None
            model = AutoModelForVision2Seq.from_pretrained(settings.model_id, **kwargs)
            processor = AutoProcessor.from_pretrained(settings.model_id)
            model.to(dev)
            model.eval()
            _cache.update({"model": model, "processor": processor, "device": dev, "quantization": quant})
            return _cache
        except Exception as exc:  # pragma: no cover
            print(f"[model_loader] failed to load {settings.model_id}: {exc}")
            return None


def load_adapter(base: Dict[str, Any], adapter_path: str) -> Optional[str]:
    """Apply a LoRA adapter to the loaded model; returns bare adapter name or None."""
    if not adapter_path or not os.path.isdir(adapter_path):
        return None
    try:
        from peft import PeftModel

        _cache["model"] = PeftModel.from_pretrained(_cache["model"], adapter_path)
        _cache["adapter"] = adapter_path
        return os.path.basename(adapter_path.rstrip("/\\"))
    except Exception as exc:  # pragma: no cover
        print(f"[model_loader] adapter load failed: {exc}")
        return None


def generate(model_obj: Dict[str, Any], messages: List[Dict[str, Any]], images: List[Any],
             max_new_tokens: int = 512, temperature: float = 0.2, do_sample: bool = True) -> str:
    processor = model_obj["processor"]
    model = model_obj["model"]
    dev = model_obj["device"]
    text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = processor(text=text, images=images, return_tensors="pt")
    inputs = {k: v.to(dev) if hasattr(v, "to") else v for k, v in inputs.items()}
    gen = model.generate(**inputs, max_new_tokens=max_new_tokens,
                         temperature=temperature, do_sample=do_sample,
                         top_p=0.9 if do_sample else None)
    return processor.batch_decode(gen[:, inputs["input_ids"].shape[-1]:], skip_special_tokens=True)[0].strip()