"""Change specialist: change-VQA and change description from a bi-temporal pair.

The mandatory multi-image task. Rather than a bespoke Siamese network, the two
acquisitions are compared quantitatively (relative radiometric normalisation,
Change Vector Analysis with a noise floor, per-class area deltas with a
significance test, a transition matrix) and the resulting numbers drive the
answer. The spatial change map that the problem statement allows as an additional
output is produced here too, as a before/after/change triptych.

The two representative queries this specialist must answer verbatim --
*"What changed between these two dates, and where did the change occur?"* and
*"Has the built-up area increased, decreased, or remained unchanged?"* -- map onto
``change_description`` and ``change_vqa`` respectively.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.analysis import render
from backend.analysis.change import measure_change
from backend.analysis.narrate import answer_change, describe_change
from backend.config import settings
from backend.controller.audit import EvidenceItem, MaskOutput
from backend.preprocessing.pipeline import PreparedImage
from backend.specialists import narration
from backend.specialists.base import (Specialist, SpecialistResult, StepRecorder,
                                      boxes_from_regions)

TASKS = ("change_vqa", "change_description")


class ChangeSpecialist(Specialist):
    """Bi-temporal change understanding with a rendered change map."""

    required_images = 2

    def __init__(self, task: str):
        if task not in TASKS:
            raise ValueError(f"{task} is not a change task")
        self.task = task
        self.display_name = {"change_vqa": "Change · bi-temporal VQA",
                             "change_description": "Change · description + change map"}[task]
        self.tool_kind = "analysis"

    def predict(self, query: str, images: List[PreparedImage],
                params: Optional[Dict[str, Any]] = None,
                run_id: str = "run") -> SpecialistResult:
        params = dict(params or {})
        if len(images) < 2:
            raise ValueError("change specialist requires a bi-temporal pair")
        t1, t2 = _chronological(images)
        rec = StepRecorder()
        t0 = time.perf_counter()
        stem = f"{run_id}_change"

        with rec.step("preprocessing.pair_alignment", self.task,
                      {"reference": Path(t1.file).name}) as step:
            step.outputs_summary = (f"t1 {t1.shape[1]}x{t1.shape[0]} ({t1.info.acquisition_date or 'undated'}) "
                                    f"vs t2 {t2.shape[1]}x{t2.shape[0]} "
                                    f"({t2.info.acquisition_date or 'undated'})")
            step.detail = {"t1": Path(t1.file).name, "t2": Path(t2.file).name,
                           "same_grid": t1.shape == t2.shape,
                           "chronology": "acquisition dates" if (t1.info.acquisition_date and
                                                                 t2.info.acquisition_date)
                           else "upload order (no dates in metadata)"}

        change_params = {
            "min_region_pixels": params.get("min_region_pixels", settings.min_region_pixels),
            "smoothing_window": params.get("smoothing_window", settings.smoothing_window),
            "change_threshold": params.get("change_threshold"),
            "significance": params.get("significance", settings.change_significance),
            "normalize_radiometry": bool(params.get("normalize_radiometry", True)),
        }
        with rec.step("analysis.change_detection", self.task, change_params) as step:
            measurement, masks = measure_change(t1, t2, **change_params)
            step.outputs_summary = (f"{measurement.changed_fraction * 100:.2f}% changed; "
                                    f"method={measurement.method}; "
                                    f"threshold={measurement.magnitude_threshold}")
            step.detail = {"per_class_delta": {d.name: d.delta_fraction for d in measurement.per_class},
                           "significant": [d.name for d in measurement.per_class if d.significant],
                           "noise_floor": measurement.noise_floor}

        with rec.step("render.change_map", self.task,
                      {"clusters": len(measurement.clusters)}) as step:
            path = render.render_change_map(
                t1.rgb, t2.rgb, masks["changed"], stem,
                clusters=measurement.clusters,
                changed_fraction=measurement.changed_fraction,
                dates=(measurement.t1_date, measurement.t2_date))
            measurement.change_map_path = path
            step.outputs_summary = Path(path).name

        evidence = [
            EvidenceItem(role="input_preview", path=t1.preview_path or "",
                         caption=f"t1 · {measurement.t1_date or 'earlier acquisition'}"),
            EvidenceItem(role="input_preview", path=t2.preview_path or "",
                         caption=f"t2 · {measurement.t2_date or 'later acquisition'}"),
            EvidenceItem(role="change_map", path=path,
                         caption="Before / after / change mask with the largest change clusters"),
        ]

        grounded_text = (describe_change(measurement) if self.task == "change_description"
                         else answer_change(query, measurement))
        signals = narration.quality_signals(measurement.quality, {
            "changed_fraction": measurement.changed_fraction,
            "change_threshold_over_noise": _threshold_ratio(measurement),
            "significant_classes": sum(1 for d in measurement.per_class if d.significant),
            "dates_known": bool(measurement.t1_date and measurement.t2_date),
        })
        spoken = narration.narrate(self.task, query, [t1, t2], measurement, params,
                                   grounded_text, rec, signals)

        return SpecialistResult(
            text=spoken.text,
            boxes=boxes_from_regions(measurement.clusters, limit=6),
            mask=MaskOutput(path=path, kind="change_map",
                            changed_fraction=measurement.changed_fraction),
            evidence=evidence, measurement=measurement, confidence_signals=signals,
            steps=rec.steps, narration_source=spoken.source, model_id=spoken.model_id,
            adapter=spoken.adapter, quantization=spoken.quantization,
            tool_kind=spoken.tool_kind, notes=spoken.notes, n_samples=spoken.n_samples,
            timing_ms=int((time.perf_counter() - t0) * 1000))


def _chronological(images: List[PreparedImage]) -> tuple[PreparedImage, PreparedImage]:
    """Order the pair by acquisition date, falling back to upload order."""
    a, b = images[0], images[1]
    da, db = a.info.acquisition_date, b.info.acquisition_date
    if da and db and da > db:
        return b, a
    return a, b


def _threshold_ratio(measurement) -> Optional[float]:
    """How far the change threshold sits above the noise floor.

    A ratio near 1 means the detected change is barely distinguishable from
    acquisition noise, which should pull the reported confidence down.
    """
    if measurement.magnitude_threshold is None or not measurement.noise_floor:
        return None
    return round(float(measurement.magnitude_threshold / max(measurement.noise_floor, 1e-6)), 3)
