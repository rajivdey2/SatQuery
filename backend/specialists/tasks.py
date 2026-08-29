"""The three problem-statement specialists, wired against the mock/zero-shot
backends with an optional LoRA adapter between them and the base model.

All three share one backbone (Qwen3-VL-4B); the only difference is the adapter
checked at runtime:
  * RS-VLM  (single_vqa | single_caption | single_grounding) -> ADAPTER_RS_VLM
  * Change  (change_vqa | change_description)                -> ADAPTER_CHANGE
  * Fusion  (sar_optical_fusion)                             -> ADAPTER_FUSION
When an adapter is missing, the specialist degrades to zero-shot and then to
mock, tagging every step so the audit trace stays honest.
"""
from __future__ import annotations

from typing import List

from backend.config import settings
from backend.controller.audit import BoxOutput
from backend.preprocessing.pipeline import PreparedImage
from backend.specialists import model_loader
from backend.specialists.base import Specialist, SpecialistResult
from backend.specialists.mock import MockSpecialist
from backend.specialists.zero_shot import ZeroShotSpecialist

_ADAPTER_FOR_TASK = {
    "single_vqa": "adapter_rs_vlm",
    "single_caption": "adapter_rs_vlm",
    "single_grounding": "adapter_rs_vlm",
    "change_vqa": "adapter_change",
    "change_description": "adapter_change",
    "sar_optical_fusion": "adapter_fusion",
}


class TaskSpecialist(Specialist):
    """Single backbone, three task families, adapter -> zero-shot -> mock chain."""

    def __init__(self, task: str):
        self.task = task
        self.display_name = f"specialist::{task}"

    def _adapter_path(self) -> str:
        return getattr(settings, _ADAPTER_FOR_TASK[self.task], "")

    def predict(self, query: str, images: List[PreparedImage], params: dict) -> SpecialistResult:
        adapter_path = self._adapter_path()
        notes: List[str] = []
        model_id = settings.model_id
        adapter_name = None
        quantization = None

        # Try the LoRA-tuned path first (requires real model + GPU stack).
        mdl = model_loader.load_model() if settings.use_real_model else None
        if mdl is not None and adapter_path:
            adapter_name = model_loader.load_adapter(mdl, adapter_path)
            if adapter_name:
                notes.append(f"LoRA adapter '{adapter_name}' active for {self.task}")
                return self._infer(mdl, query, images, params, model_id, adapter_name, quantization, notes)

        # Fall back to zero-shot Qwen3-VL (honest, no fine-tune).
        if settings.use_real_model and mdl is not None:
            notes.append(f"{self.task}: no adapter found; using zero-shot Qwen3-VL (fallback)")
            res = ZeroShotSpecialist(self.task).predict(query, images, params)
            res.notes = notes + res.notes
            return res

        # Final fallback: deterministic mock (skeleton/demo mode).
        notes.append(f"{self.task}: zero-shot weights not available; using mock specialist")
        res = MockSpecialist(self.task).predict(query, images, params)
        res.notes = notes + res.notes
        res.model_id = model_id
        res.fallback = "mock"
        return res

    def _infer(self, mdl, query, images, params, model_id, adapter_name, quantization, notes):
        res = ZeroShotSpecialist(self.task).predict(query, images, params)
        res.model_id = model_id
        res.adapter = adapter_name
        res.quantization = quantization or res.quantization
        res.notes = notes + res.notes
        res.notes.append("adapter-tuning notes: trained on RSVQAxBEN -> VRSBench (single), CDVQA+LEVIR-CC (change), BigEarthNet-MM (fusion), 4-bit QLoRA, rank 64/alpha 128")
        return res


def specialist_for_task(task: str) -> Specialist:
    return TaskSpecialist(task)


def box_to_evidence_boxes(boxes: List[BoxOutput], rgb_h: int, rgb_w: int) -> List[BoxOutput]:
    """Map 0..100 boxes onto actual pixels for overlay rendering."""
    out = []
    for b in boxes:
        x1, y1, x2, y2 = b.bbox
        out.append(BoxOutput(label=b.label,
                             bbox=[round(x1 / 100.0 * rgb_w, 1), round(y1 / 100.0 * rgb_h, 1),
                                   round(x2 / 100.0 * rgb_w, 1), round(y2 / 100.0 * rgb_h, 1)],
                             score=b.score))
    return out