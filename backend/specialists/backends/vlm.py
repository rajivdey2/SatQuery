"""Evidence-grounded VLM narration.

The VLM is not the measuring instrument here -- the analysis engine is. When a
Qwen3-VL backend (optionally with a LoRA adapter from ``training/``) is available,
it is handed the image(s) *and* the measurement record and asked to phrase an
answer that stays inside those numbers. That ordering is deliberate: it keeps the
fluency of a VLM while keeping the arithmetic checkable, and it means switching
the VLM off degrades the wording rather than the correctness.

Two confidence signals come out of this module, both real:
``logit_margin`` (mean top-2 token probability margin) and ``self_consistency``
(agreement across repeated samples when sampling is enabled).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.config import settings
from backend.specialists.backends import model_loader

_SYSTEM = (
    "You are SatQuery AI, a remote-sensing analysis assistant working with Indian Space "
    "Research Organisation imagery (Cartosat-2S optical, RISAT SAR) and public benchmark "
    "imagery. A measurement engine has already analysed the image(s) and given you its "
    "numbers. Rules: (1) state the answer directly in at most four sentences; (2) use only "
    "the supplied measurements for any number, percentage, area, count or direction -- never "
    "invent or round-trip your own; (3) if the measurements do not answer the question, say "
    "exactly what is missing; (4) do not describe your reasoning process."
)

_GROUNDING_NOTE = (
    "Bounding boxes have already been computed by the measurement engine and are given to you. "
    "Describe what was localised and where; do not output your own coordinates."
)

_TASK_FRAMING = {
    "single_vqa": "Answer the user's question about this single remote-sensing image.",
    "single_caption": "Describe the land cover and the major objects visible in this image.",
    "single_grounding": "Report the localisation result for the user's referring expression.",
    "change_vqa": ("Image 1 is the earlier acquisition and image 2 the later acquisition of the "
                   "same area. Answer the user's change question."),
    "change_description": ("Image 1 is the earlier acquisition and image 2 the later acquisition of "
                           "the same area. Describe what changed and where."),
    "sar_optical_fusion": ("Image 1 is optical and image 2 is SAR of the same area, co-registered. "
                           "Answer using the joint evidence from both sensors."),
}


@dataclass
class VlmNarration:
    text: str
    logit_margin: Optional[float] = None
    self_consistency: Optional[float] = None
    n_samples: int = 1
    model_id: str = ""
    adapter: Optional[str] = None
    quantization: Optional[str] = None
    duration_ms: int = 0
    notes: List[str] = field(default_factory=list)


def available() -> bool:
    return model_loader.available()


def _compact(value: Any, depth: int = 0) -> Any:
    """Trim a measurement dump to what a language model can actually use."""
    if isinstance(value, dict):
        drop = {"indices", "regions", "t1", "t2", "optical", "sar", "land_cover",
                "transitions", "clusters", "scene_labels", "agreement", "joint_classes",
                "per_class", "classes", "quality", "complementarity"}
        out = {}
        for k, v in value.items():
            if depth == 0 and k in drop:
                continue
            out[k] = _compact(v, depth + 1)
        return out
    if isinstance(value, list):
        return [_compact(v, depth + 1) for v in value[:6]]
    if isinstance(value, float):
        return round(value, 4)
    return value


def evidence_block(measurement: Any, extra: Optional[Dict[str, Any]] = None) -> str:
    """Render the measurement record as the model's evidence section."""
    payload: Dict[str, Any] = {}
    if measurement is not None:
        dump = getattr(measurement, "model_dump", None)
        raw = dump(mode="json") if dump else dict(measurement)
        payload["headline"] = _compact(raw)
        for key in ("classes", "per_class", "joint_classes", "agreement"):
            rows = raw.get(key)
            if rows:
                payload[key] = [_compact(r, 1) for r in rows[:8]]
        quality = raw.get("quality") or {}
        if quality:
            payload["limitations"] = quality.get("limitations", [])[:4]
            payload["indices_unavailable"] = quality.get("indices_unavailable", [])[:6]
        for key in ("transitions", "clusters", "regions", "complementarity", "scene_labels"):
            rows = raw.get(key)
            if rows:
                payload[key] = [_compact(r, 1) for r in rows[:4]]
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, indent=1, default=str)


def _agreement(texts: List[str]) -> Optional[float]:
    """Token-overlap agreement across samples -- the self-consistency signal."""
    if len(texts) < 2:
        return None
    sets = [set(re.findall(r"[a-z0-9.%]+", t.lower())) for t in texts]
    scores = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            union = sets[i] | sets[j]
            if not union:
                continue
            scores.append(len(sets[i] & sets[j]) / len(union))
    if not scores:
        return None
    return round(sum(scores) / len(scores), 4)


def narrate(task: str, query: str, images: List[Any], measurement: Any,
            params: Optional[Dict[str, Any]] = None, adapter_path: str = "",
            grounded_answer: str = "", extra_evidence: Optional[Dict[str, Any]] = None
            ) -> Optional[VlmNarration]:
    """Narrate the measurements with the VLM, or return None if unavailable."""
    mdl = model_loader.load_model()
    if mdl is None:
        return None
    params = params or {}
    t0 = time.perf_counter()
    notes: List[str] = []

    adapter = model_loader.load_adapter(mdl, adapter_path) if adapter_path else None
    if adapter_path and not adapter:
        notes.append(f"Adapter '{adapter_path}' was requested but could not be loaded; "
                     "running the base model zero-shot.")

    framing = _TASK_FRAMING.get(task, "Answer the user's question about the image(s).")
    if task == "single_grounding":
        framing += " " + _GROUNDING_NOTE
    user = (f"{framing}\n\nUser query: {query or '(no query supplied)'}\n\n"
            f"Measurements from the analysis engine (authoritative):\n"
            f"{evidence_block(measurement, extra_evidence)}\n\n"
            f"Grounded reference answer derived from those measurements:\n{grounded_answer}\n\n"
            "Rewrite the reference answer as a direct, natural response. Keep every number "
            "exactly as given. If a caveat is listed, keep the important one.")
    messages = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]

    pil_images = _to_pil(images)
    max_tokens = int(params.get("max_new_tokens", settings.max_new_tokens))
    temperature = float(params.get("temperature", settings.temperature))
    n_samples = max(1, int(params.get("self_consistency_samples", settings.self_consistency_samples)))

    texts: List[str] = []
    margins: List[float] = []
    try:
        for i in range(n_samples):
            do_sample = temperature > 0.0 and (n_samples > 1 or temperature > 0.0)
            text, margin = model_loader.generate(
                mdl, messages, pil_images, max_new_tokens=max_tokens,
                temperature=temperature if do_sample else 0.0, do_sample=do_sample)
            if text:
                texts.append(text)
            if margin is not None:
                margins.append(margin)
    except Exception as exc:  # pragma: no cover - runtime GPU failures
        print(f"[vlm] generation failed: {exc}")
        return None

    if not texts:
        return None
    return VlmNarration(
        text=texts[0], logit_margin=(round(sum(margins) / len(margins), 4) if margins else None),
        self_consistency=_agreement(texts), n_samples=len(texts),
        model_id=settings.model_id, adapter=adapter,
        quantization=mdl.get("quantization"),
        duration_ms=int((time.perf_counter() - t0) * 1000), notes=notes)


def _to_pil(images: List[Any]) -> List[Any]:
    """Accept ``PreparedImage`` or raw arrays and hand PIL images to the processor."""
    from PIL import Image

    out = []
    for im in images:
        arr = getattr(im, "rgb", im)
        out.append(Image.fromarray(arr) if not hasattr(arr, "save") else arr)
    return out
