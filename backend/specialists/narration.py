"""Shared narration and confidence-signal plumbing for the specialists.

All three specialists follow the same contract: measure, render, then phrase.
This module holds the "then phrase" half plus the extraction of the confidence
signals, so the specialists themselves stay about their own domain and the
fallback chain (VLM -> measured text -> explicit mock) behaves identically
everywhere.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from backend.analysis.scene_labels import label_margin
from backend.config import settings
from backend.specialists.backends import vlm

ANALYSIS_MODEL_ID = "satquery-analysis-engine-v1"

_ADAPTER_FOR_TASK = {
    "single_vqa": "adapter_rs_vlm",
    "single_caption": "adapter_rs_vlm",
    "single_grounding": "adapter_rs_vlm",
    "change_vqa": "adapter_change",
    "change_description": "adapter_change",
    "sar_optical_fusion": "adapter_fusion",
}


class Narration:
    """Outcome of the narration stage."""

    def __init__(self, text: str, source: str, model_id: str, adapter: Optional[str] = None,
                 quantization: Optional[str] = None, n_samples: int = 1,
                 notes: Optional[List[str]] = None):
        self.text = text
        self.source = source
        self.model_id = model_id
        self.adapter = adapter
        self.quantization = quantization
        self.n_samples = n_samples
        self.notes = notes or []

    @property
    def tool_kind(self) -> str:
        if self.source == "mock":
            return "mock"
        return "vlm" if self.source.startswith("vlm") else "analysis"


def adapter_path_for(task: str) -> str:
    return getattr(settings, _ADAPTER_FOR_TASK.get(task, ""), "") or ""


def quality_signals(quality, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Turn a ``QualityReport`` into the raw signals the confidence stage weighs."""
    signals: Dict[str, Any] = {
        "separability": quality.separability,
        "valid_pixel_fraction": quality.valid_pixel_fraction,
        "band_completeness": round(quality.band_completeness, 4),
        "spatial_reference": quality.spatial_reference,
        "n_limitations": len(quality.limitations),
    }
    if extra:
        signals.update(extra)
    return signals


def learned_signals(land_cover) -> Dict[str, Any]:
    """Signals contributed by the adapted BigEarthNet-MM head, when present."""
    if land_cover is None or not getattr(land_cover, "scene_labels", None):
        return {}
    preds = land_cover.scene_labels
    return {"learned_top1": preds[0]["probability"],
            "learned_margin": label_margin(preds),
            "learned_source": land_cover.label_source}


def narrate(task: str, query: str, images: List[Any], measurement: Any,
            params: Dict[str, Any], grounded_text: str, recorder,
            signals: Dict[str, Any],
            extra_evidence: Optional[Dict[str, Any]] = None) -> Narration:
    """Rewrite the measured answer with a VLM when one is available.

    Order of preference: explicit mock mode (labelled as such), VLM narration
    constrained to the measurements, then the measured text itself. The measured
    text is never discarded -- it is what the VLM is asked to rephrase, so a VLM
    failure costs fluency, not correctness.
    """
    if settings.use_mock:
        from backend.specialists.backends.mock import MockSpecialist

        # Hand the real measured answer to the mock so the demo text reflects the
        # numbers the analysis engine actually produced, while staying explicitly
        # tagged as mock phrasing (never mistaken for a measured model).
        mock = MockSpecialist(task).predict(query, images, params,
                                            measured_text=grounded_text)
        return Narration(mock.text, "mock", "mock-specialist", notes=mock.notes)

    if not vlm.available():
        return Narration(grounded_text, "measurement", ANALYSIS_MODEL_ID)

    with recorder.step("narration.vlm", task,
                       {"model_id": settings.model_id,
                        "max_new_tokens": params.get("max_new_tokens", settings.max_new_tokens),
                        "temperature": params.get("temperature", settings.temperature),
                        "adapter": adapter_path_for(task) or None}) as step:
        narration = vlm.narrate(task, query, images, measurement, params,
                                adapter_path=adapter_path_for(task),
                                grounded_answer=grounded_text,
                                extra_evidence=extra_evidence)
        if narration is None:
            step.status = "skipped"
            step.outputs_summary = "VLM backend unavailable at call time; kept the measured answer"
            return Narration(grounded_text, "measurement", ANALYSIS_MODEL_ID,
                             notes=["VLM narration requested but unavailable; "
                                    "answer is the measured text."])
        step.outputs_summary = (f"{len(narration.text)} chars; logit margin "
                                f"{narration.logit_margin}; {narration.n_samples} sample(s)")
        signals["logit_margin"] = narration.logit_margin
        signals["self_consistency"] = narration.self_consistency
        return Narration(narration.text, "vlm+measurement", narration.model_id,
                         adapter=narration.adapter, quantization=narration.quantization,
                         n_samples=narration.n_samples, notes=narration.notes)
