"""Common specialist interface.

Every specialist (mock, zero-shot Qwen3-VL, or LoRA-tuned adapter) exposes
`predict(query, images, params)` and returns a `SpecialistResult`. The problem
statement's "select one or more models or tools from a predefined registry" maps
1:1 onto the task->specialist registry in controller/registry.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from backend.controller.audit import BoxOutput, MaskOutput


@dataclass
class SpecialistResult:
    text: str = ""
    boxes: List[BoxOutput] = field(default_factory=list)
    mask: MaskOutput = field(default_factory=MaskOutput)
    confidence: Optional[float] = None
    confidence_source: str = "not_available"
    confidence_note: str = ""
    model_id: str = "mock"
    adapter: Optional[str] = None
    quantization: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    timing_ms: Optional[int] = None


class Specialist:
    """Base class. `task` is one of the six problem-statement tasks."""

    task: str = ""
    display_name: str = ""
    fallback: Optional[str] = None

    def predict(self, query: str, images: list, params: dict) -> SpecialistResult:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"task": self.task, "display_name": self.display_name,
                "model_id": self._model_id(), "adapter": self._adapter(),
                "fallback": self.fallback}

    def _model_id(self) -> str:
        return "mock"

    def _adapter(self) -> Optional[str]:
        return None


def parse_boxes(text: str, image_width: int, image_height: int) -> List[BoxOutput]:
    """Parse Qwen-style `<box>(x1,y1,x2,y2)</box>` (0..1000) into normalized 0..100 boxes."""
    import re

    from backend.controller.audit import BoxOutput

    out: List[BoxOutput] = []
    for m in re.finditer(r"<box>\s*\((\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?)\)\s*</box>", text):
        x1, y1, x2, y2 = (float(g) for g in m.groups())
        # Prompted models may output in 0..100 space already; normalize defensively.
        if max(x1, y1, x2, y2) > 100.0:
            x1, y1, x2, y2 = (v / 10.0 for v in (x1, y1, x2, y2))
        out.append(BoxOutput(label="", bbox=[min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]))
    return out