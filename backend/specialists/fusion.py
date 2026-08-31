"""Optical-SAR fusion specialist: joint extraction from a co-registered pair.

The third mandatory regime, and the one CLAUDE.md section 6 identifies as the
differentiator. Both modalities are measured independently and then compared:
per-class Cohen's kappa and IoU, an obscured-area analysis showing where the radar
sees what the optical sensor cannot, and a joint class map whose provenance is
recorded per class.

Cross-modal agreement is also the confidence signal for this task. Two physically
independent sensors agreeing on where the water is means something; when they
disagree the answer says so rather than averaging the disagreement into a
confident-looking number.

Answers the representative query *"Use the optical and SAR images together to
identify built-up and water-covered regions."*
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.analysis import render
from backend.analysis.fusion import measure_fusion
from backend.analysis.measurements import BUILT_UP, WATER
from backend.analysis.narrate import answer_fusion
from backend.config import settings
from backend.controller.audit import EvidenceItem, MaskOutput
from backend.preprocessing.pipeline import PreparedImage
from backend.specialists import narration
from backend.specialists.base import (Specialist, SpecialistResult, StepRecorder,
                                      boxes_from_regions)

TASKS = ("sar_optical_fusion",)


class FusionSpecialist(Specialist):
    """Dual-branch optical + SAR joint information extraction."""

    required_images = 2

    def __init__(self, task: str = "sar_optical_fusion"):
        if task not in TASKS:
            raise ValueError(f"{task} is not the fusion task")
        self.task = task
        self.display_name = "Fusion · optical + SAR joint extraction"
        self.tool_kind = "analysis"

    def predict(self, query: str, images: List[PreparedImage],
                params: Optional[Dict[str, Any]] = None,
                run_id: str = "run") -> SpecialistResult:
        params = dict(params or {})
        if len(images) < 2:
            raise ValueError("fusion specialist requires an optical+SAR pair")
        optical, sar = _split_modalities(images)
        rec = StepRecorder()
        t0 = time.perf_counter()
        stem = f"{run_id}_fusion"

        with rec.step("preprocessing.modality_split", self.task, {}) as step:
            step.outputs_summary = (f"optical={Path(optical.file).name} ({optical.bands.layout}), "
                                    f"sar={Path(sar.file).name} ({sar.bands.layout})")
            step.detail = {
                "optical_bands": optical.bands.available(),
                "sar_channels": sorted(sar.sar_channels),
                "sar_radiometry": {k: v.convention for k, v in sar.sar_channels.items()},
                "sar_enl": {k: round(v.enl, 2) for k, v in sar.sar_channels.items()},
                "speckle_filtered": all(v.speckle_filtered for v in sar.sar_channels.values())
                if sar.sar_channels else False,
            }

        fusion_params = {
            "min_region_pixels": params.get("min_region_pixels", settings.min_region_pixels),
            "smoothing_window": params.get("smoothing_window", settings.smoothing_window),
            "regions_per_class": int(params.get("regions_per_class", 4)),
        }
        with rec.step("analysis.optical_sar_fusion", self.task, fusion_params) as step:
            measurement, masks = measure_fusion(optical, sar, **fusion_params)
            step.outputs_summary = (
                f"mean cross-modal kappa={measurement.mean_agreement}; "
                f"joint water={_frac(measurement, WATER):.3f}, built-up={_frac(measurement, BUILT_UP):.3f}; "
                f"obscured={measurement.obscured_fraction:.3f}")
            step.detail = {"agreement": [s.model_dump() for s in measurement.agreement],
                           "complementarity": measurement.complementarity}

        with rec.step("render.fusion_map", self.task, {}) as step:
            fusion_path = render.render_fusion_map(
                optical.rgb, masks, stem,
                stats={c.name: c.fraction for c in measurement.joint_classes},
                obscured=masks.get("obscured"))
            measurement.overlay_path = fusion_path
            pair_path = render.render_side_by_side(
                optical.rgb, sar.rgb, f"{stem}_pair",
                labels=(f"optical · {Path(optical.file).name}", f"SAR · {Path(sar.file).name}"))
            step.outputs_summary = f"{Path(fusion_path).name}, {Path(pair_path).name}"

        evidence = [
            EvidenceItem(role="input_preview", path=optical.preview_path or "",
                         caption=f"optical · {Path(optical.file).name}"),
            EvidenceItem(role="input_preview", path=sar.preview_path or "",
                         caption=f"SAR · {Path(sar.file).name} "
                                 f"({', '.join(sorted(sar.sar_channels)) or 'no channel'})"),
            EvidenceItem(role="side_by_side", path=pair_path,
                         caption="Co-registered optical / SAR pair after SAR calibration"),
            EvidenceItem(role="fusion_map", path=fusion_path,
                         caption="Joint optical+SAR classes; white outline marks optically obscured area"),
        ]

        grounded_text = answer_fusion(query, measurement)
        signals = narration.quality_signals(measurement.quality, {
            "cross_modal_kappa": measurement.mean_agreement,
            "obscured_fraction": measurement.obscured_fraction,
            "sar_enl": min((v.enl for v in sar.sar_channels.values()), default=None),
            "sar_radiometry_known": all(v.convention != "unitless"
                                        for v in sar.sar_channels.values()) if sar.sar_channels else False,
        })
        signals.update(narration.learned_signals(measurement.optical))
        spoken = narration.narrate(self.task, query, [optical, sar], measurement, params,
                                   grounded_text, rec, signals)

        return SpecialistResult(
            text=spoken.text,
            boxes=boxes_from_regions(measurement.regions, limit=8),
            mask=MaskOutput(path=fusion_path, kind="fusion_map"),
            evidence=evidence, measurement=measurement, confidence_signals=signals,
            steps=rec.steps, narration_source=spoken.source, model_id=spoken.model_id,
            adapter=spoken.adapter, quantization=spoken.quantization,
            tool_kind=spoken.tool_kind, notes=spoken.notes, n_samples=spoken.n_samples,
            timing_ms=int((time.perf_counter() - t0) * 1000))


def _split_modalities(images: List[PreparedImage]) -> Tuple[PreparedImage, PreparedImage]:
    """Return ``(optical, sar)`` regardless of upload order."""
    sar = next((im for im in images if im.is_sar), None)
    optical = next((im for im in images if not im.is_sar), None)
    if sar is None or optical is None:
        # The validator normally prevents this; keep a deterministic fallback so a
        # mislabelled product produces a traceable answer instead of a crash.
        return images[0], images[1]
    return optical, sar


def _frac(measurement, class_name: str) -> float:
    stat = next((c for c in measurement.joint_classes if c.name == class_name), None)
    return stat.fraction if stat else 0.0
