"""Predefined model/tool registry (Controller stage 3).

Problem statement: the controller selects models/tools "from a predefined
registry" -- this module is exactly that registry. Each task declares required
inputs and the *permitted* parameter set; the executor must not pass anything
else to a specialist.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from backend.config import settings


@dataclass
class RegistryEntry:
    id: str
    task: str
    required_inputs: str          # single_image | pair | optical_sar_pair | bi_temporal_pair
    permitted_params: Dict[str, object] = field(default_factory=dict)
    description: str = ""


def _base_params() -> Dict[str, object]:
    return {"max_new_tokens": settings.max_new_tokens, "temperature": settings.temperature}


REGISTRY: List[RegistryEntry] = [
    RegistryEntry(id="rs_vlm.vqa", task="single_vqa", required_inputs="single_image",
                  permitted_params=_base_params(),
                  description="Qwen3-VL-4B (+ optional RSVQAxBEN/VRSBench LoRA). Free-form single-image QA."),
    RegistryEntry(id="rs_vlm.caption", task="single_caption", required_inputs="single_image",
                  permitted_params=_base_params(),
                  description="Qwen3-VL-4B captioning specialist (VRSBench captioning)."),
    RegistryEntry(id="rs_vlm.grounding", task="single_grounding", required_inputs="single_image",
                  permitted_params=_base_params(),
                  description="Qwen3-VL-4B native grounding -> 0..100 bounding boxes (VRSBench/RSVG)."),
    RegistryEntry(id="change.vqa", task="change_vqa", required_inputs="bi_temporal_pair",
                  permitted_params=_base_params(),
                  description="Two-image interleaved prompt (CDVQA, LEVIR-CC, QAG-360K LoRA)."),
    RegistryEntry(id="change.description", task="change_description", required_inputs="bi_temporal_pair",
                  permitted_params=_base_params(),
                  description="Change captioning: what changed + where (LEVIR-CC)."),
    RegistryEntry(id="fusion.joint", task="sar_optical_fusion", required_inputs="optical_sar_pair",
                  permitted_params=_base_params(),
                  description="Optical+SAR dual-encoder/MMM fusion (BigEarthNet-MM LoRA; zero-shot fallback)."),
]

_BY_TASK = {e.task: e for e in REGISTRY}


def entry_for_task(task: str) -> Optional[RegistryEntry]:
    return _BY_TASK.get(task)


def filter_permitted_params(task: str, extra: Optional[Dict[str, object]]) -> Dict[str, object]:
    entry = entry_for_task(task)
    if not entry:
        return dict(_base_params())
    allowed = set(entry.permitted_params.keys())
    base = {k: v for k, v in _base_params().items() if k in allowed}
    if extra:
        for k, v in extra.items():
            if k in allowed:
                base[k] = v
    return base