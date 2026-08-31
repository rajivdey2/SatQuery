"""Audit-trail schema and logger — the graded artefact.

Problem statement 26167 says only the observable execution trace is evaluated:
the selected task, the models/tools chosen from the predefined registry, the
permitted parameters, the outputs and the confidence. This module is therefore
the contract for that trace, not a debug log. It is versioned, rendered in the
GUI, embedded in the downloadable PDF, and written to disk for every run
including rejected ones.

Schema 2.0.0 adds, relative to 1.0.0:
  * ``plan`` and ``steps`` — the sequence of tools actually executed with their
    per-step parameters and timings, so multi-tool routing is observable.
  * ``measurements`` — the numeric evidence behind the answer.
  * ``evidence`` — every rendered artefact with its role.
  * ``confidence.components`` — the individual signals behind the score.
  * ``environment`` — which backend actually ran.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from backend.config import TRACE_DIR

AUDIT_SCHEMA_VERSION = "2.0.0"

TASKS = ("single_vqa", "single_caption", "single_grounding",
         "change_vqa", "change_description", "sar_optical_fusion")

ConfidenceSource = Literal[
    "measurement_composite",     # separability / coverage of the analysis engine
    "cross_modal_agreement",     # Cohen's kappa between optical and SAR evidence
    "learned_probability",       # adapted BigEarthNet-MM head margin
    "logit_margin",              # VLM top-2 token margin
    "self_consistency",          # agreement across sampled VLM answers
    "validator_gate",            # deterministic rejection
    "mock",                      # placeholder, explicitly labelled
    "not_available",
]


class InputImage(BaseModel):
    filename: str
    format: str                                   # GEOTIFF | TIFF | PNG | JPEG
    modality: str                                 # optical | multispectral | sar | panchromatic | unknown
    modality_confidence: str = "low"
    modality_reason: str = ""
    width: int = 0
    height: int = 0
    band_count: int = 0
    dtype: str = ""
    crs: Optional[str] = None
    extent: Optional[List[float]] = None
    extent_wgs84: Optional[List[float]] = None
    pixel_size: Optional[List[float]] = None
    pixel_area_m2: Optional[float] = None
    acquisition_date: Optional[str] = None
    metadata_source: Literal["rasterio_tags", "filename", "none"] = "none"
    sensor_guess: Optional[str] = None
    band_assignment: Dict[str, Any] = Field(default_factory=dict)
    details: Dict[str, Any] = Field(default_factory=dict)


class InputConfig(BaseModel):
    n_images: int
    images: List[InputImage] = Field(default_factory=list)
    geolocated: bool = False
    co_registered: Optional[bool] = None
    co_registration_note: Optional[str] = None
    bi_temporal: Optional[bool] = None
    cross_modal: Optional[bool] = None
    dates_differ: Optional[bool] = None
    date_note: Optional[str] = None
    modality_signature: List[str] = Field(default_factory=list)


class ValidationCheck(BaseModel):
    name: str
    status: Literal["passed", "warning", "failed"]
    message: str
    detail: Dict[str, Any] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    passed: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    failed: List[str] = Field(default_factory=list)
    checks: List[ValidationCheck] = Field(default_factory=list)
    input_config: InputConfig
    accepted: bool = True


class RegistryEntryUsed(BaseModel):
    id: str
    task: str
    tool_kind: str = "analysis"                   # analysis | vlm | adapted_head | mock
    model_id: str = ""
    adapter: Optional[str] = None
    quantization: Optional[str] = None
    fallback: Optional[str] = None
    permitted_parameters: Dict[str, Any] = Field(default_factory=dict)
    rejected_parameters: List[str] = Field(default_factory=list)


class ToolStep(BaseModel):
    """One executed tool call inside the selected workflow."""

    order: int
    tool: str
    task: str = ""
    parameters: Dict[str, Any] = Field(default_factory=dict)
    outputs_summary: str = ""
    duration_ms: int = 0
    status: Literal["ok", "skipped", "failed"] = "ok"
    detail: Dict[str, Any] = Field(default_factory=dict)


class ConfidenceComponent(BaseModel):
    name: str
    value: Optional[float] = None
    weight: float = 0.0
    note: str = ""


class Confidence(BaseModel):
    value: Optional[float] = None
    source: ConfidenceSource = "not_available"
    calibrated: bool = False
    components: List[ConfidenceComponent] = Field(default_factory=list)
    note: str = ""


class TextOutput(BaseModel):
    text: str = ""
    narration_source: str = "measurement"         # measurement | vlm | vlm+measurement | mock | validator
    n_samples: int = 1


class BoxOutput(BaseModel):
    label: str = ""
    bbox: List[float] = Field(default_factory=list)   # [x1,y1,x2,y2], 0..100 normalised
    score: Optional[float] = None
    class_name: Optional[str] = None
    area_ha: Optional[float] = None
    position: Optional[str] = None


class MaskOutput(BaseModel):
    path: Optional[str] = None
    kind: Literal["change_map", "class_map", "fusion_map", "segmentation", "none"] = "none"
    changed_fraction: Optional[float] = None


class EvidenceItem(BaseModel):
    role: str                                     # input_preview | class_map | grounding_overlay | ...
    path: str
    caption: str = ""


class Outputs(BaseModel):
    text: TextOutput = Field(default_factory=TextOutput)
    boxes: List[BoxOutput] = Field(default_factory=list)
    mask: MaskOutput = Field(default_factory=MaskOutput)
    evidence: List[EvidenceItem] = Field(default_factory=list)
    evidence_thumbnails: List[str] = Field(default_factory=list)


class AuditTrace(BaseModel):
    """The graded execution trace. Bump AUDIT_SCHEMA_VERSION on breaking changes."""

    schema_version: str = AUDIT_SCHEMA_VERSION
    run_id: str = ""
    task: str
    query: str = ""
    query_hash: str = ""
    input_config: InputConfig
    validation: ValidationReport
    classification: Dict[str, Any] = Field(default_factory=dict)
    plan: List[str] = Field(default_factory=list)
    registry_entries_used: List[RegistryEntryUsed] = Field(default_factory=list)
    steps: List[ToolStep] = Field(default_factory=list)
    effective_parameters: Dict[str, Any] = Field(default_factory=dict)
    measurements: Dict[str, Any] = Field(default_factory=dict)
    outputs: Outputs = Field(default_factory=Outputs)
    confidence: Confidence = Field(default_factory=Confidence)
    execution_time_ms: Optional[int] = None
    model_backend: str = "analysis_engine"
    environment: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    server_version: str = ""

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    def summary(self) -> dict:
        """Compact form for list views and the report header."""
        return {"task": self.task, "run_id": self.run_id,
                "tools": [e.id for e in self.registry_entries_used],
                "confidence": {"value": self.confidence.value, "source": self.confidence.source},
                "execution_time_ms": self.execution_time_ms,
                "validation": {"accepted": self.validation.accepted,
                               "warnings": len(self.validation.warnings),
                               "failed": len(self.validation.failed)}}


def log_trace(trace: AuditTrace, run_id: str, trace_dir: Optional[Path] = None) -> Path:
    path = Path(trace_dir or TRACE_DIR) / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(trace.to_json(), encoding="utf-8")
    return path


def load_trace(run_id: str, trace_dir: Optional[Path] = None) -> Optional[dict]:
    path = Path(trace_dir or TRACE_DIR) / f"{run_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def now_ms() -> int:
    return int(time.time() * 1000)


def image_to_audit(info, prepared=None) -> InputImage:
    """Build an ``InputImage`` from a probe result (and optionally its prepared form)."""
    band_assignment: Dict[str, Any] = {}
    details: Dict[str, Any] = {}
    pixel_area = None
    if prepared is not None:
        band_assignment = prepared.bands.to_audit()
        pixel_area = prepared.pixel_area_m2
        details = {"analysis_shape": [prepared.shape[0], prepared.shape[1]],
                   "read_decimation": round(prepared.read_scale, 3),
                   "valid_pixel_fraction": round(float(prepared.valid.mean()), 4)}
        if prepared.sar_channels:
            details["sar_channels"] = {k: v.to_audit() for k, v in prepared.sar_channels.items()}
        if prepared.notes:
            details["preprocessing_notes"] = list(prepared.notes)
    return InputImage(
        filename=Path(info.path).name, format=info.format, modality=info.modality,
        modality_confidence=info.modality_confidence, modality_reason=info.modality_reason,
        width=info.width, height=info.height, band_count=info.band_count, dtype=info.dtype,
        crs=info.crs, extent=info.extent, extent_wgs84=info.extent_wgs84,
        pixel_size=info.pixel_size, pixel_area_m2=pixel_area,
        acquisition_date=info.acquisition_date,
        metadata_source=(info.metadata_source if info.metadata_source in
                         ("rasterio_tags", "filename", "none") else "none"),
        sensor_guess=info.sensor_guess, band_assignment=band_assignment, details=details)
