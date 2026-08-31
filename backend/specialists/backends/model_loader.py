"""Lazy Qwen3-VL loader with a real confidence signal.

Only activates when ``USE_REAL_MODEL=1`` and torch+transformers are importable, so
the API keeps serving the measurement engine on machines without a GPU stack.

The one thing this module does beyond loading: ``generate`` optionally returns the
mean top-2 token probability margin. That is the ``logit_margin`` confidence
source in the audit trace -- a genuine signal from the model's own output
distribution, in contrast to a hand-picked constant.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Optional, Tuple

from backend.config import settings

_lock = threading.Lock()
_cache: Dict[str, Any] = {}


def available() -> bool:
    """True when a VLM backend could actually run here."""
    if not settings.use_real_model or not settings.has_torch:
        return False
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401

        return True
    except Exception:
        return False


def load_model() -> Optional[Dict[str, Any]]:
    """Return ``{'model','processor','device','quantization'}`` or None."""
    if not available():
        return None
    with _lock:
        if "model" in _cache:
            return _cache
        try:
            import torch
            from transformers import AutoProcessor

            try:                                    # Qwen3-VL / Qwen2.5-VL native class
                from transformers import AutoModelForImageTextToText as _AutoVLM
            except ImportError:                     # older transformers
                from transformers import AutoModelForVision2Seq as _AutoVLM

            dev = settings.device if settings.device != "auto" else (
                "cuda" if torch.cuda.is_available() else "cpu")
            quant = None
            kwargs: Dict[str, Any] = {
                "dtype": torch.float16 if dev == "cuda" else torch.float32,
                "low_cpu_mem_usage": True,
            }
            if settings.quantize_bits in (4, 8) and dev == "cuda":
                try:
                    from transformers import BitsAndBytesConfig

                    kwargs["quantization_config"] = BitsAndBytesConfig(
                        load_in_4bit=settings.quantize_bits == 4,
                        load_in_8bit=settings.quantize_bits == 8,
                        bnb_4bit_compute_dtype=torch.float16,
                        bnb_4bit_quant_type="nf4")
                    quant = f"{settings.quantize_bits}bit-bnb"
                except Exception:
                    quant = None
            model = _AutoVLM.from_pretrained(settings.model_id, **kwargs)
            processor = AutoProcessor.from_pretrained(settings.model_id)
            if quant is None:
                model = model.to(dev)
            model.eval()
            _cache.update({"model": model, "processor": processor, "device": dev,
                           "quantization": quant, "adapters": {}})
            return _cache
        except Exception as exc:  # pragma: no cover - depends on host GPU stack
            print(f"[model_loader] failed to load {settings.model_id}: {exc}")
            return None


def load_adapter(base: Dict[str, Any], adapter_path: str) -> Optional[str]:
    """Apply a LoRA adapter; returns its name, or None when unavailable."""
    if not adapter_path or not os.path.isdir(adapter_path):
        return None
    name = os.path.basename(adapter_path.rstrip("/\\"))
    with _lock:
        loaded = _cache.setdefault("adapters", {})
        if name in loaded:
            return name
        try:
            from peft import PeftModel

            _cache["model"] = PeftModel.from_pretrained(_cache["model"], adapter_path)
            loaded[name] = adapter_path
            return name
        except Exception as exc:  # pragma: no cover
            print(f"[model_loader] adapter load failed for {adapter_path}: {exc}")
            return None


def _content(messages: List[Dict[str, Any]], n_images: int) -> List[Dict[str, Any]]:
    """Insert image placeholders into the chat template content blocks."""
    out: List[Dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "user":
            blocks = [{"type": "image"} for _ in range(n_images)]
            blocks.append({"type": "text", "text": m.get("content", "")})
            out.append({"role": "user", "content": blocks})
        else:
            out.append({"role": m["role"], "content": [{"type": "text", "text": m.get("content", "")}]})
    return out


def generate(model_obj: Dict[str, Any], messages: List[Dict[str, Any]], images: List[Any],
             max_new_tokens: int = 512, temperature: float = 0.2,
             do_sample: Optional[bool] = None,
             with_margin: bool = True) -> Tuple[str, Optional[float]]:
    """Run the VLM once. Returns ``(text, mean_top2_probability_margin)``."""
    import torch

    processor = model_obj["processor"]
    model = model_obj["model"]
    dev = model_obj["device"]
    sample = (temperature > 0.0) if do_sample is None else bool(do_sample)

    chat = _content(messages, len(images))
    text = processor.apply_chat_template(chat, tokenize=False, add_generation_prompt=True)
    inputs = processor(text=[text], images=images or None, return_tensors="pt", padding=True)
    inputs = {k: (v.to(dev) if hasattr(v, "to") else v) for k, v in inputs.items()}

    gen_kwargs: Dict[str, Any] = {"max_new_tokens": int(max_new_tokens), "do_sample": sample}
    if sample:
        gen_kwargs.update({"temperature": float(temperature), "top_p": 0.9})
    if with_margin:
        gen_kwargs.update({"output_scores": True, "return_dict_in_generate": True})

    with torch.inference_mode():
        out = model.generate(**inputs, **gen_kwargs)

    prompt_len = inputs["input_ids"].shape[-1]
    sequences = out.sequences if with_margin else out
    decoded = processor.batch_decode(sequences[:, prompt_len:], skip_special_tokens=True)[0].strip()

    margin = None
    if with_margin and getattr(out, "scores", None):
        margins = []
        for step_scores in out.scores:
            probs = torch.softmax(step_scores[0].float(), dim=-1)
            top2 = torch.topk(probs, k=2).values
            margins.append(float(top2[0] - top2[1]))
        if margins:
            margin = round(sum(margins) / len(margins), 4)
    return decoded, margin


def describe() -> dict:
    """Registry-facing description of the narration backend."""
    if not settings.use_real_model:
        return {"available": False, "reason": "USE_REAL_MODEL is not set; "
                                              "answers come from the measurement engine"}
    if not available():
        return {"available": False, "reason": "torch/transformers not installed in this environment"}
    loaded = "model" in _cache
    return {"available": True, "model_id": settings.model_id, "loaded": loaded,
            "device": _cache.get("device", settings.device),
            "quantization": _cache.get("quantization"),
            "adapters": sorted(_cache.get("adapters", {}))}
