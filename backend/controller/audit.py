"""Audit-trail schema and logger.

The JSON execution trace is a first-class deliverable (Problem Statement 26167:
only the observable execution trace is evaluated). The schema below is versioned
and stable; it is rendered in the GUI and embedded in the downloadable PDF report.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

from backend.config import TRACE_DIR

AUDIT_SCHEMA_VERSION = "1.0.0"


class InputImage(BaseModel):
    filename: str
    format: str  # GEOTIFF | TIFF | PNG | JPEG
    modality: str  # optical | multispectral | sar | panchromatic | unknown
    width: int
    height: int
    band_count: int
    dtype: str
    crs: Optional[str] = None
    extent: Optional[List[float]] = None  # [minx, miny, maxx, maxy]
    pixel_size: Optional[List[float]] = None  # [x, y]
    acquisition_date: Optional[str] = None
    metadata_source: Literal["rasterio_tags", "filename", "none"] = "none"


class InputConfig(BaseModel):
    n_images: int
    images: List[InputImage]
    geolocated: bool
    co_registered: Optional[bool] = None
    co_registration_note: Optional[str] = None
    bi_temporal: Optional[bool] = None
    dates_differ: Optional[bool] = None
    date_note: Optional[str] = None
    modality_signature: List[str] = Field(default_factory=list)


class ValidationCheck(BaseModel):
    name: str
    status: Literal["passed", "warning", "failed"]
    message: str


class ValidationReport(BaseModel):
    passed: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    failed: List[str] = Field(default_factory=list)
    checks: List[ValidationCheck] = Field(default_factory=list)
    input_config: InputConfig
    accepted: bool


class RegistryEntryUsed(BaseModel):
    id: str
    task: str
    model_id: str
    adapter: Optional[str] = None
    quantization: Optional[str] = None
    fallback: Optional[str] = None
    permitted_parameters: dict[str, Any] = Field(default_factory=dict)


class Confidence(BaseModel):
    value: Optional[float] = None
    source: Literal["logit_margin", "self_consistency", "validator_gate", "mock", "not_available"] = "not_available"
    note: str = ""


class TextOutput(BaseModel):
    text: str
    n_shots: int = 1


class BoxOutput(BaseModel):
    label: str
    bbox: List[float]  # [xmin, ymin, xmax, ymax] in 0..100 normalized coords
    score: Optional[float] = None
    tile_index: Optional[int] = None


class MaskOutput(BaseModel):
    path: Optional[str] = None
    kind: Literal["change_map", "segmentation", "none"] = "none"


class Outputs(BaseModel):
    text: TextOutput = Field(default_factory=lambda: TextOutput(text=""))
    boxes: List[BoxOutput] = Field(default_factory=list)
    mask: MaskOutput = MaskOutput()
    evidence_thumbnails: List[str] = Field(default_factory=list)


class AuditTrace(BaseModel):
    """The graded execution trace. Field names are stable; bump AUDIT_SCHEMA_VERSION on breaking changes."""

    schema_version: str = AUDIT_SCHEMA_VERSION
    task: str
    query: str
    query_hash: str = ""
    input_config: InputConfig
    classification: dict[str, Any] = Field(default_factory=dict)
    validation: ValidationReport
    registry_entries_used: List[RegistryEntryUsed] = Field(default_factory=list)
    execution_time_ms: Optional[int] = None
    confidence: Confidence = Confidence()
    outputs: Outputs = Outputs()
    model_backend: str = "mock"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    server_version: str = ""

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)


def log_trace(trace: AuditTrace, run_id: str) -> Path:
    path = TRACE_DIR / f"{run_id}.json"
    path.write_text(trace.to_json(), encoding="utf-8")
    return path


def now_ms() -> int:
    return int(time.time() * 1000)