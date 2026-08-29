"""Combiner + confidence (Controller stage 5).

Confidence policy (see CLAUDE.md risk register): only report a number where a
real signal exists. The current backends give us:
  * mock            -> value is a placeholder, clearly labeled source="mock"
  * zero-shot       -> no calibrated signal -> not_available, say so
  * LoRA adapter    -> logit-margin machinery not yet built -> not_available, say so
  * validator gate  -> the rejection branch is a genuine (deterministic) signal
"""
from __future__ import annotations

from backend.controller.audit import Confidence, Outputs, RegistryEntryUsed, TextOutput
from backend.specialists.base import SpecialistResult
from backend.specialists.tasks import box_to_evidence_boxes


def build_confidence(result: SpecialistResult) -> Confidence:
    if result.confidence_source == "mock":
        return Confidence(value=result.confidence, source="mock",
                          note="Deterministic placeholder (skeleton/demo mode), not a calibrated score.")
    if result.mask.kind != "none" and result.mask.path:
        return Confidence(value=None, source="validated_overlay",
                          note="Spatial overlay produced by a dedicated change-map specialist; numeric score not yet calibrated.")
    # Adapter or zero-shot: report honestly.
    note = ("Fine-tuned adapter active but top-2 logit-margin confidence is not computed yet "
            "for this task; reporting 'not available' instead of fabricating a number.")
    if result.adapter:
        return Confidence(value=None, source="not_available", note=note)
    return Confidence(value=None, source="not_available",
                      note="Zero-shot model response; no calibrated confidence signal available for this task.")


def combine(result: SpecialistResult, preview_paths: list[str]) -> Outputs:
    boxes = [b for b in result.boxes]
    return Outputs(text=TextOutput(text=result.text),
                   boxes=boxes,
                   mask=result.mask,
                   evidence_thumbnails=[str(p) for p in preview_paths])


def registry_entry_used(result: SpecialistResult, entry_id: str, task: str, params: dict) -> RegistryEntryUsed:
    return RegistryEntryUsed(id=entry_id, task=task, model_id=result.model_id,
                             adapter=result.adapter, quantization=result.quantization,
                             fallback=result.fallback,
                             permitted_parameters=params)