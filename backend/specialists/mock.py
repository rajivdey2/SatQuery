"""Deterministic mock specialist.

Used so the entire graded core (controller, validator, audit, GUI, PDF) is
demoable before any weights exist. All mock outputs are explicitly tagged in the
audit trace and carry the confidence_source "mock".
"""
from __future__ import annotations

import time
from typing import List

from backend.controller.audit import BoxOutput
from backend.specialists.base import Specialist, SpecialistResult
from backend.preprocessing.pipeline import PreparedImage


class MockSpecialist(Specialist):
    def __init__(self, task: str):
        self.task = task
        self.display_name = f"mock::{task}"

    def _model_id(self) -> str:
        return "mock-qwen3vl-4b"

    def predict(self, query: str, images: List[PreparedImage], params: dict) -> SpecialistResult:
        t0 = time.time()
        mods = [im.info.modality for im in images]
        dates = [im.info.acquisition_date for im in images if im.info.acquisition_date]
        suffix = " [mock output - wired to real specialist once adapters ship]"
        text, boxes = self._answer(query, mods, dates)
        elapsed = int((time.time() - t0) * 1000)
        return SpecialistResult(
            text=text + suffix,
            boxes=boxes,
            confidence=0.80,
            confidence_source="mock",
            confidence_note="Deterministic placeholder; replace via adapter or zero-shot backend.",
            model_id=self._model_id(),
            notes=["mock specialist (no weights loaded)"],
            timing_ms=elapsed,
        )

    def _answer(self, query: str, mods: List[str], dates: List[str]):
        mod = mods[0] if mods else "optical"
        q = query or ""
        if self.task == "single_vqa":
            return (f"In the {mod} scene, the dominant land-cover signals are developed and "
                    f"agricultural/bare-surface areas, with scattered water-like regions. "
                    f"Relevant to: '{q}'."), []
        if self.task == "single_caption":
            return (f"The {mod} image shows an area of mixed land cover: residential development, "
                    f"open agricultural fields, a network of roads, and a water body on the eastern side."), []
        if self.task == "single_grounding":
            box = BoxOutput(label=q or "object", bbox=[35.0, 30.0, 65.0, 60.0], score=0.8)
            return (f"The object(s) matching '{q}' are localised in the highlighted box (center of scene)."), [box]
        if self.task == "change_vqa":
            trend = "increased" if "increase" in q.lower() else ("decreased" if "decrease" in q.lower() else "remained similar")
            return (f"Based on the bi-temporal pair{dates and ' (%s -> %s)' % tuple(dates) or ''}, the built-up area has {trend}."), []
        if self.task == "change_description":
            span = f" between {dates[0]} and {dates[1]}" if len(dates) == 2 else ""
            return (f"The main change{span} is new building construction concentrated near the "
                    f"northwest corner, plus a small vegetation-to-bare-soil conversion along the riverbank."), []
        if self.task == "sar_optical_fusion":
            return (f"Using optical and SAR evidence jointly:{'' if not mods else ' modalities [' + ', '.join(mods) + ']' } "
                    f"built-up regions and water-covered regions are identified with high agreement across both sensors."), []
        return (f"Processed as {self.task} for: '{q}'."), []