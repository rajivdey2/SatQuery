"""Task Classifier (Controller stage 2).

Deterministic keyword + input-config rules first (demo-safe, zero-latency),
with an optional few-shot LLM hook for ambiguity. Returns one of the six
problem-statement tasks and records the method used in the audit trace.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from backend.controller.audit import InputConfig

TASKS = [
    "single_vqa",
    "single_caption",
    "single_grounding",
    "change_vqa",
    "change_description",
    "sar_optical_fusion",
]

_CHANGE_WORDS = [
    "change", "changed", "changes", "difference", "differ", "between the two",
    "before and after", "temporal", "compared", "compare", "increase", "decreased",
    "remained unchanged", "grown", "what happened",
]
_GROUND_WORDS = [
    "highlight", "highlight the", "where is", "where are", "locate", "point to",
    "point at", "show me", "find the", "which region", "bounding box", "bounding boxes",
    "box", "ground", "grounded", "referring", "position of", "coordinates of",
]
_CAPTION_WORDS = [
    "describe", "description", "caption", "describe the", "what can you see",
    "what do you see", "scene description", "land-cover and major objects visible",
    "summarize the image", "overview of", "explain the image",
]
_FUSION_WORDS = [
    "optical and sar", "sar and optical", "both images", "together", "cross-modal",
    "cross modal", "fusion", "combine", "combined", "jointly", "multi-sensor",
    "multisensor", "use the optical and sar",
]
_QUESTION_LEADERS = re.compile(r"^(what|which|has|have|did|is|are|was|were|how|where|when|who|does|do)\b", re.I)


class ClassificationResult:
    def __init__(self, task: str, method: str, reasons: List[str], alternatives: Dict[str, float] = None):
        self.task = task
        self.method = method
        self.reasons = reasons
        self.alternatives = alternatives or {}

    def to_audit(self) -> dict:
        return {"task": self.task, "method": self.method, "reasons": self.reasons,
                "alternatives": self.alternatives}


def _has_any(text: str, words: List[str]) -> bool:
    return any(w in text for w in words)


def classify(query: str, input_config: Optional[InputConfig] = None, llm_hook=None) -> ClassificationResult:
    q = " " + query.lower().strip() + " "
    reasons: List[str] = []

    n = input_config.n_images if input_config else 1
    signature = input_config.modality_signature if input_config else []

    fusion = _has_any(q, _FUSION_WORDS) and n == 2 and "sar" in signature
    cross_modal = n == 2 and "sar" in signature and any(m in ("optical", "multispectral") for m in signature)
    change = _has_any(q, _CHANGE_WORDS)
    grounding = _has_any(q, _GROUND_WORDS)
    question = bool(_QUESTION_LEADERS.search(q.strip()))

    if fusion:
        reasons.append("query asks to use optical+SAR together; cross-modal pair detected")
        return _pick("sar_optical_fusion", "rules", reasons)

    if change:
        if n < 2:
            reasons.append("query asks about change but only one image provided; falling back to single-image VQA")
            return _pick("single_vqa", "rules", reasons + ["image_count_constraint"])
        if question:
            reasons.append("change intent + interrogative structure")
            return _pick("change_vqa", "rules", reasons)
        reasons.append("change intent + descriptive structure")
        return _pick("change_description", "rules", reasons)

    if grounding:
        reasons.append("grounding intent keywords ('highlight', 'where is', ...)")
        return _pick("single_grounding", "rules", reasons)

    if _has_any(q, _CAPTION_WORDS):
        reasons.append("description/caption intent keywords")
        return _pick("single_caption", "rules", reasons)

    if question or n == 1:
        reasons.append("default single-image VQA for interrogative/general queries")
        return _pick("single_vqa", "rules", reasons)

    # Ambiguous fallback: ask the LLM hook if present, else default to VQA.
    if llm_hook is not None:
        task = llm_hook(query, TASKS)
        if task in TASKS:
            reasons.append(f"LLM few-shot classification -> {task}")
            return _pick(task, "llm", reasons)
    reasons.append("default to single-image VQA")
    return _pick("single_vqa", "rules", reasons)


def _pick(task: str, method: str, reasons: List[str]) -> ClassificationResult:
    alternatives = {t: 0.0 for t in TASKS}
    alternatives[task] = 1.0
    return ClassificationResult(task=task, method=method, reasons=reasons, alternatives=alternatives)