"""RS-VLM specialist: single-image VQA, captioning and text-guided grounding.

Covers the mandatory single-image baseline (VQA) plus the second single-image
task. Both captioning and grounding are implemented -- grounding is the one
CLAUDE.md section 4 recommends because it returns visual evidence, and it is the
task the problem statement's second representative query asks for.

The specialist measures first and speaks second: land cover and, for grounding,
connected target regions are measured from the raw bands, rendered as overlays,
and only then turned into a sentence -- by the narration templates, or by
Qwen3-VL constrained to those numbers when a VLM backend is enabled.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.analysis import render
from backend.analysis.grounding import ground_query
from backend.analysis.landcover import measure_land_cover
from backend.analysis.narrate import answer_grounding, answer_single, describe_scene
from backend.config import settings
from backend.controller.audit import EvidenceItem, MaskOutput
from backend.preprocessing.pipeline import PreparedImage
from backend.specialists import narration
from backend.specialists.base import (Specialist, SpecialistResult, StepRecorder,
                                      boxes_from_regions)

TASKS = ("single_vqa", "single_caption", "single_grounding")


class RsVlmSpecialist(Specialist):
    """One backbone, three single-image task heads."""

    required_images = 1

    def __init__(self, task: str):
        if task not in TASKS:
            raise ValueError(f"{task} is not a single-image task")
        self.task = task
        self.display_name = {"single_vqa": "RS-VLM · single-image VQA",
                             "single_caption": "RS-VLM · scene description",
                             "single_grounding": "RS-VLM · text-guided grounding"}[task]
        self.tool_kind = "analysis"

    # -- main entry ----------------------------------------------------------
    def predict(self, query: str, images: List[PreparedImage],
                params: Optional[Dict[str, Any]] = None,
                run_id: str = "run") -> SpecialistResult:
        params = dict(params or {})
        if not images:
            raise ValueError("single-image specialist requires one image")
        img = images[0]
        rec = StepRecorder()
        t0 = time.perf_counter()
        stem = f"{run_id}_{Path(img.file).stem}"

        with rec.step("preprocessing.prepare_image", self.task,
                      {"speckle_filter": settings.speckle_filter if img.is_sar else None,
                       "band_roles": img.bands.roles}) as step:
            step.outputs_summary = (f"{img.info.modality} {img.bands.layout}, "
                                    f"{img.shape[1]}x{img.shape[0]} analysis grid, "
                                    f"{len(img.bands.available())} resolved band roles")
            step.detail = {"band_assignment_source": img.bands.source,
                           "pixel_area_m2": img.pixel_area_m2}

        lc_params = {"min_region_pixels": params.get("min_region_pixels", settings.min_region_pixels),
                     "smoothing_window": params.get("smoothing_window", settings.smoothing_window)}
        with rec.step("analysis.land_cover", self.task, lc_params) as step:
            lc = measure_land_cover(img, **lc_params)
            step.outputs_summary = (f"dominant={lc.measurement.dominant}; "
                                    f"indices={','.join(lc.measurement.quality.indices_available)}; "
                                    f"separability={lc.measurement.quality.separability}")
            step.detail = {"class_fractions": {c.name: c.fraction for c in lc.measurement.classes}}

        evidence: List[EvidenceItem] = [
            EvidenceItem(role="input_preview", path=img.preview_path or "",
                         caption=f"{img.info.modality} input · {Path(img.file).name}")]
        boxes = []
        mask_out = MaskOutput()
        measurement = lc.measurement

        if self.task == "single_grounding":
            g_params = {"target_class": params.get("target_class"),
                        "max_regions": int(params.get("max_regions", 5)),
                        "min_region_pixels": lc_params["min_region_pixels"]}
            with rec.step("analysis.grounding", self.task, g_params) as step:
                grounding, aux = ground_query(img, query, land_cover=lc, **g_params)
                step.outputs_summary = (f"target={grounding.target_class}; "
                                        f"{len(grounding.regions)} region(s) selected of "
                                        f"{grounding.candidates_considered} candidates")
                step.detail = {"qualifier": grounding.qualifier, "method": grounding.method}
            boxes = boxes_from_regions(grounding.regions)
            with rec.step("render.grounding_overlay", self.task, {}) as step:
                path = render.draw_boxes(img.rgb, grounding.regions, f"{stem}_grounding",
                                         mask=aux.get("mask"))
                grounding.overlay_path = path
                step.outputs_summary = Path(path).name
            evidence.append(EvidenceItem(role="grounding_overlay", path=grounding.overlay_path,
                                         caption="Localised region(s) with class outline"))
            measurement = grounding
            grounded_text = answer_grounding(grounding)
        else:
            with rec.step("render.class_map", self.task, {}) as step:
                path = render.render_class_map(
                    img.rgb, lc.masks, f"{stem}_classmap",
                    stats={c.name: c.fraction for c in lc.measurement.classes})
                step.outputs_summary = Path(path).name
            evidence.append(EvidenceItem(role="class_map", path=path,
                                         caption="Measured land-cover classes"))
            mask_out = MaskOutput(path=path, kind="class_map")
            grounded_text = (describe_scene(lc.measurement, img.info.modality)
                             if self.task == "single_caption"
                             else answer_single(query, lc.measurement, img.info.modality))

        signals = _signals(lc, measurement)
        spoken = narration.narrate(self.task, query, [img], measurement, params,
                                   grounded_text, rec, signals)

        return SpecialistResult(
            text=spoken.text, boxes=boxes, mask=mask_out, evidence=evidence,
            measurement=measurement, confidence_signals=signals, steps=rec.steps,
            narration_source=spoken.source, model_id=spoken.model_id, adapter=spoken.adapter,
            quantization=spoken.quantization, tool_kind=spoken.tool_kind,
            notes=spoken.notes, n_samples=spoken.n_samples,
            timing_ms=int((time.perf_counter() - t0) * 1000))


def _signals(lc, measurement) -> Dict[str, Any]:
    """Collect the real confidence signals available from a single-image pass."""
    quality = getattr(measurement, "quality", None) or lc.measurement.quality
    signals = narration.quality_signals(quality)
    signals.update(narration.learned_signals(lc.measurement))
    target = getattr(measurement, "target_class", None)
    if target:
        stat = lc.measurement.get(target)
        if stat:
            signals["target_reliability"] = stat.reliability
            signals["target_fraction"] = stat.fraction
        regions = getattr(measurement, "regions", []) or []
        if regions:
            signals["region_score"] = regions[0].score
        else:
            signals["region_score"] = 0.0
    return signals
