"""Common specialist interface.

Every specialist -- single-image RS-VLM, change, optical+SAR fusion -- implements
``predict(query, images, params)`` and returns a ``SpecialistResult``. That single
signature is what lets the controller treat them as interchangeable entries in the
predefined registry (CLAUDE.md section 2), and what lets a mock, a measurement
engine and a fine-tuned VLM occupy the same slot without the controller noticing.

A result carries more than text: the numeric measurement it was derived from, the
rendered visual evidence, the individual confidence signals (never a fabricated
score), and the tool steps executed inside the specialist so the audit trace can
show the real sequence.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.controller.audit import BoxOutput, EvidenceItem, MaskOutput, ToolStep
from backend.preprocessing.pipeline import PreparedImage


@dataclass
class SpecialistResult:
    """Everything one specialist produced, ready for the combiner."""

    text: str = ""
    boxes: List[BoxOutput] = field(default_factory=list)
    mask: MaskOutput = field(default_factory=MaskOutput)
    evidence: List[EvidenceItem] = field(default_factory=list)
    measurement: Optional[Any] = None                    # pydantic measurement record
    confidence_signals: Dict[str, Any] = field(default_factory=dict)
    steps: List[ToolStep] = field(default_factory=list)
    narration_source: str = "measurement"
    model_id: str = ""
    adapter: Optional[str] = None
    quantization: Optional[str] = None
    fallback: Optional[str] = None
    tool_kind: str = "analysis"
    notes: List[str] = field(default_factory=list)
    timing_ms: Optional[int] = None
    n_samples: int = 1

    def measurement_dict(self) -> Dict[str, Any]:
        if self.measurement is None:
            return {}
        dump = getattr(self.measurement, "model_dump", None)
        return dump(mode="json") if dump else dict(self.measurement)


class StepRecorder:
    """Collects ``ToolStep`` entries so the trace shows the real execution order."""

    def __init__(self) -> None:
        self.steps: List[ToolStep] = []

    @contextmanager
    def step(self, tool: str, task: str = "", parameters: Optional[Dict[str, Any]] = None,
             detail: Optional[Dict[str, Any]] = None):
        entry = ToolStep(order=len(self.steps) + 1, tool=tool, task=task,
                         parameters=dict(parameters or {}), detail=dict(detail or {}))
        self.steps.append(entry)
        t0 = time.perf_counter()
        try:
            yield entry
        except Exception as exc:
            entry.status = "failed"
            entry.outputs_summary = f"{type(exc).__name__}: {exc}"
            entry.duration_ms = int((time.perf_counter() - t0) * 1000)
            raise
        entry.duration_ms = int((time.perf_counter() - t0) * 1000)


class Specialist:
    """Base class. ``task`` is one of the six problem-statement tasks."""

    task: str = ""
    display_name: str = ""
    tool_kind: str = "analysis"
    fallback: Optional[str] = None

    #: How many images this specialist requires, for the registry's input check.
    required_images: int = 1

    def predict(self, query: str, images: List[PreparedImage],
                params: Optional[Dict[str, Any]] = None,
                run_id: str = "run") -> SpecialistResult:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"task": self.task, "display_name": self.display_name,
                "tool_kind": self.tool_kind, "required_images": self.required_images,
                "fallback": self.fallback}


def boxes_from_regions(regions, limit: int = 8) -> List[BoxOutput]:
    """``Region`` measurements -> audit-trace boxes in 0..100 coordinates."""
    out: List[BoxOutput] = []
    for r in regions[:limit]:
        if len(r.bbox_norm) != 4:
            continue
        out.append(BoxOutput(label=r.label, bbox=[float(v) for v in r.bbox_norm],
                             score=r.score, class_name=r.class_name or None,
                             area_ha=r.area_ha, position=r.position or None))
    return out


def denormalize_boxes(boxes: List[BoxOutput], width: int, height: int) -> List[List[float]]:
    """0..100 boxes -> pixel coordinates (report rendering, IoU against pixel GT)."""
    out = []
    for b in boxes:
        if len(b.bbox) != 4:
            continue
        x1, y1, x2, y2 = b.bbox
        out.append([round(x1 / 100.0 * width, 1), round(y1 / 100.0 * height, 1),
                    round(x2 / 100.0 * width, 1), round(y2 / 100.0 * height, 1)])
    return out
