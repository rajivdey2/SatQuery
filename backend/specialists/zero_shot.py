"""Zero-shot Qwen3-VL specialist.

Serves every task from the base model with task-specific system prompts and
multi-image input, so the controller is demoable with real inference the moment
a GPU box with the model appears -- before any LoRA adapter exists. Adapter
loading is layered on top by the task specialists in specialists/tasks.py.
"""
from __future__ import annotations

import re
import time
from typing import List

import numpy as np

from backend.controller.audit import BoxOutput
from backend.preprocessing.pipeline import PreparedImage
from backend.specialists import model_loader
from backend.specialists.base import Specialist, SpecialistResult

_SYS_BASE = (
    "You are SatQuery AI, a remote-sensing vision-language assistant for Indian Space "
    "Research Organisation (ISRO) satellite imagery (Cartosat-2S optical, RISAT SAR). "
    "Answer concisely and only about what is visible in the image(s). "
    "If you are not confident, say so explicitly."
)

_SYS_GROUNDING = (
    _SYS_BASE + " For object localisation, output exactly one <box>(x1,y1,x2,y2)</box> "
    "per mentioned instance using relative coordinates 0-100, e.g. <box>(10,20,40,50)</box>."
)


def _prompt(task: str, query: str, mods: List[str]) -> List[dict]:
    n = len(mods)
    if task == "single_vqa":
        user = f"Answer the question about this single remote-sensing image.\nQuestion: {query}"
    elif task == "single_caption":
        user = "Describe the land-cover and the major objects visible in this single remote-sensing image."
    elif task == "single_grounding":
        user = f"Localise the object(s) referred to by: \"{query}\". Output a <box>(x1,y1,x2,y2)</box>."
    elif task in ("change_vqa", "change_description"):
        user = (
            "Image 1 is the earlier acquisition, Image 2 is the later acquisition of the same area.\n"
            + ("Answer the change question: " + query if task == "change_vqa" else
               "Describe what changed between the two acquisitions and where the change occurred.")
        )
    else:  # sar_optical_fusion
        user = (
            "Image 1 is optical, Image 2 is SAR of the same area.\n"
            f"Use both together to answer: {query}"
        )
    system = _SYS_GROUNDING if task == "single_grounding" else _SYS_BASE
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


class ZeroShotSpecialist(Specialist):
    def __init__(self, task: str):
        self.task = task
        self.display_name = f"zero-shot::{task}"

    def _model_id(self) -> str:
        return model_loader.settings.model_id if model_loader.settings else "Qwen/Qwen3-VL-4B-Instruct"

    def predict(self, query: str, images: List[PreparedImage], params: dict) -> SpecialistResult:
        t0 = time.time()
        mdl = model_loader.load_model()
        if not mdl:
            return SpecialistResult(
                text="", confidence=None, confidence_source="not_available",
                confidence_note="Zero-shot backend requested but model is not loaded (needs USE_REAL_MODEL=1 and GPU stack).",
                model_id=self._model_id(), notes=["zero-shot unavailable -> runtime fallback"],
                timing_ms=int((time.time() - t0) * 1000))

        import PIL.Image

        mods = [im.info.modality for im in images]
        if not images:
            raise ValueError("Specialist requires at least one image.")
        pil_images = [PIL.Image.fromarray(im.rgb) for im in images]
        max_tokens = int(params.get("max_new_tokens", 512))
        temperature = float(params.get("temperature", 0.2))
        text = model_loader.generate(mdl, _prompt(self.task, query, mods), pil_images,
                                     max_new_tokens=max_tokens, temperature=temperature)

        boxes: List[BoxOutput] = []
        if self.task == "single_grounding" and images:
            h, w = images[0].rgb.shape[:2]
            for m in re.finditer(r"<box>\s*\(([\d.]+),([\d.]+),([\d.]+),([\d.]+)\)", text):
                x1, y1, x2, y2 = [float(g) for g in m.groups()]
                if max(x1, y1, x2, y2) > 100:  # 0..1000 space -> 0..100
                    x1, y1, x2, y2 = [v / 10.0 for v in (x1, y1, x2, y2)]
                boxes.append(BoxOutput(label=query or "object", bbox=[min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)], score=0.5))

        return SpecialistResult(
            text=text, boxes=boxes, confidence=None, confidence_source="not_available",
            confidence_note="Zero-shot: model reports next-token sampling; no calibrated confidence signal; see combiner.",
            model_id=self._model_id(), adapter=None, quantization=mdl.get("quantization"),
            notes=["zero-shot Qwen3-VL (no fine-tune)"],
            timing_ms=int((time.time() - t0) * 1000))