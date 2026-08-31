"""Deterministic mock backend.

Kept only as an explicit escape hatch (``SATQUERY_MOCK=1``) for demoing the
controller, GUI and report plumbing on a machine where even numpy-level analysis
is undesirable -- for example a UI-only walkthrough.

The mock is *not* a dumb placeholder: when real analysis already ran upstream, its
measured answer (``measured_text``) is folded into the mock phrasing so the demo
reads like a real answer. Even then every mock answer is tagged
``narration_source="mock"`` and ``confidence.source="mock"`` so it can never be
mistaken for a measurement in the audit trace -- the honesty contract in CLAUDE.md
section 14 is preserved no matter how presentable the text looks.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from backend.controller.audit import BoxOutput, MaskOutput
from backend.specialists.base import Specialist, SpecialistResult

# A deliberately small, fixed region box per task so the demo always renders a
# grounding overlay without relying on analysis output. Coordinates are the
# 0..100 normalised space the audit trace uses.
_DEMO_BOX = BoxOutput(label="region", class_name="water",
                      bbox=[38.0, 32.0, 62.0, 58.0], score=0.5, area_ha=None,
                      position="central-lower")


class MockSpecialist(Specialist):
    tool_kind = "mock"

    def __init__(self, task: str):
        self.task = task
        self.display_name = f"mock::{task}"
        self.required_images = 2 if task in ("change_vqa", "change_description",
                                             "sar_optical_fusion") else 1

    def predict(self, query: str, images: List[Any], params: Optional[Dict[str, Any]] = None,
                run_id: str = "run", measured_text: str = "") -> SpecialistResult:
        t0 = time.perf_counter()
        mods = [getattr(im.info, "modality", "optical") for im in images]
        text, boxes = self._answer(query or "", mods, measured_text or "")
        disclaimer = " [Mock output — SATQUERY_MOCK is on; phrasing is template, not a measured model.]"
        return SpecialistResult(
            text=text.rstrip() + disclaimer,
            boxes=boxes, mask=MaskOutput(),
            confidence_signals={"mock": True},
            narration_source="mock", model_id="mock-specialist", tool_kind="mock",
            fallback="mock", notes=["mock backend enabled via SATQUERY_MOCK"],
            timing_ms=int((time.perf_counter() - t0) * 1000))

    def _answer(self, query: str, mods: List[str], measured: str):
        mod = mods[0] if mods else "optical"
        n = len(mods)

        # When real analysis produced a number-backed sentence, lead with it. This
        # is what makes the mock demo truthful (the numbers are real) yet clearly
        # mock-phrased (the prose is templated).
        if measured.strip():
            lead = f"Based on the measured signal, {self._clean(measured)}"
        else:
            lead = self._fallback_lead(query, mod, n)

        if self.task == "single_grounding":
            return (
                f"{lead} The region of interest is centred toward the lower part of the "
                f"scene (see the outlined box below).",
                [_DEMO_BOX],
            )
        if self.task == "single_caption":
            return (f"{lead} The scene is an {mod} acquisition; the dominant cover types and "
                    f"any objects are summarised after the measurements.", [])
        if self.task == "change_description":
            return (f"{lead} Between the two acquisitions the changed-area clusters are marked "
                    f"on the change map; the per-class direction of change is in the table.", [])
        if self.task == "change_vqa":
            return (f"{lead} This is a bi-temporal change verdict; the direction per class is "
                    f"listed in the measurements.", [])
        if self.task == "sar_optical_fusion":
            return (f"{lead} Fusing the {mods[0]} and {mods[-1]} channels, the built-up and "
                    f"water-covered extents are separated in the joint map below.", [])
        return f"{lead} Answer for '{query}'.", []

    @staticmethod
    def _clean(measured: str) -> str:
        measured = " ".join(measured.split())
        if len(measured) > 240:
            measured = measured[:237].rstrip() + "…"
        if not measured.endswith((".", "!", "?")):
            measured += "."
        return measured[0].lower() + measured[1:]

    @staticmethod
    def _fallback_lead(query: str, mod: str, n: int) -> str:
        if n >= 2:
            return "The inputs are a co-registered / bi-temporal pair."
        return f"For “{query.strip()}” on the {mod} scene"

    def describe(self) -> dict:
        d = super().describe()
        d["mock"] = True
        d["note"] = "template phrasing; may embed measured text when available"
        return d
