"""Combiner (Controller stage 5, first half).

Merges the textual answer with the spatial outputs (boxes, masks, rendered
overlays) into the single ``Outputs`` block the GUI and the report read, and
records which registry entry ran with which permitted parameters. Confidence
itself lives in ``controller/confidence.py``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.controller.audit import (Confidence, EvidenceItem, Outputs, RegistryEntryUsed,
                                      TextOutput)
from backend.controller.confidence import estimate
from backend.controller.registry import RegistryEntry
from backend.specialists.base import SpecialistResult


def combine(result: SpecialistResult, preview_paths: Optional[List[str]] = None) -> Outputs:
    """Assemble the answer, the spatial evidence and every rendered artefact."""
    evidence: List[EvidenceItem] = [e for e in result.evidence if e.path]
    seen = {e.path for e in evidence}
    for p in preview_paths or []:
        if p and p not in seen:
            evidence.append(EvidenceItem(role="input_preview", path=p,
                                         caption=Path(p).name))
            seen.add(p)
    return Outputs(
        text=TextOutput(text=result.text, narration_source=result.narration_source,
                        n_samples=result.n_samples),
        boxes=list(result.boxes), mask=result.mask, evidence=evidence,
        evidence_thumbnails=[e.path for e in evidence])


def build_confidence(result: SpecialistResult) -> Confidence:
    return estimate(result.confidence_signals, result.narration_source)


def registry_entry_used(entry: RegistryEntry, result: SpecialistResult,
                        params: Dict[str, Any],
                        rejected: Optional[List[str]] = None) -> RegistryEntryUsed:
    return RegistryEntryUsed(
        id=entry.id, task=entry.task, tool_kind=result.tool_kind,
        model_id=result.model_id, adapter=result.adapter,
        quantization=result.quantization, fallback=result.fallback,
        permitted_parameters=dict(params), rejected_parameters=list(rejected or []))


def measurement_block(result: SpecialistResult) -> Dict[str, Any]:
    """The numeric evidence, JSON-ready for the audit trace."""
    return result.measurement_dict()
