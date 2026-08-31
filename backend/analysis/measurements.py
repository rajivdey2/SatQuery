"""Typed measurement records.

The problem statement grades the *observable execution trace*: selected task,
models/tools, permitted parameters and outputs. Free prose is not evidence, so
every specialist in this system produces one of the records below -- measured
quantities with units, the threshold that produced them, and an explicit account
of what could not be measured. The narration layer is then only allowed to
restate these numbers, which is what makes the answers checkable.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

# Canonical land-cover vocabulary used across all specialists.
WATER = "water"
VEGETATION = "vegetation"
BUILT_UP = "built_up"
BARE_SOIL = "bare_soil"
OTHER = "other"
CLASS_ORDER = (WATER, VEGETATION, BUILT_UP, BARE_SOIL, OTHER)

CLASS_LABELS = {
    WATER: "water",
    VEGETATION: "vegetation",
    BUILT_UP: "built-up / man-made surface",
    BARE_SOIL: "bare soil or fallow ground",
    OTHER: "unclassified",
}


class IndexStat(BaseModel):
    """One spectral index, its distribution, and the threshold applied to it."""

    name: str
    available: bool = True
    mean: Optional[float] = None
    std: Optional[float] = None
    p05: Optional[float] = None
    p95: Optional[float] = None
    threshold: Optional[float] = None
    threshold_method: Optional[str] = None
    separability: Optional[float] = None      # Otsu eta in [0,1]; higher = cleaner split
    note: str = ""


class ClassStat(BaseModel):
    """Per-class extent, with the evidence it rests on."""

    name: str
    label: str = ""
    pixel_count: int = 0
    fraction: float = 0.0                     # of valid pixels, 0..1
    area_ha: Optional[float] = None
    evidence_basis: str = ""                  # which bands/indices decided this class
    reliability: str = "medium"               # high | medium | low
    region_count: int = 0


class Region(BaseModel):
    """A connected region -- the spatial evidence returned for grounding."""

    label: str
    class_name: str = ""
    bbox_norm: List[float] = Field(default_factory=list)    # [x1,y1,x2,y2] in 0..100
    bbox_px: List[int] = Field(default_factory=list)
    centroid_norm: List[float] = Field(default_factory=list)
    area_px: int = 0
    area_ha: Optional[float] = None
    fraction: float = 0.0
    position: str = ""
    compactness: Optional[float] = None
    score: float = 0.0


class QualityReport(BaseModel):
    """What the measurement could and could not see -- input to confidence."""

    band_layout: str = ""
    bands_available: List[str] = Field(default_factory=list)
    indices_available: List[str] = Field(default_factory=list)
    indices_unavailable: List[str] = Field(default_factory=list)
    valid_pixel_fraction: float = 1.0
    separability: Optional[float] = None
    spatial_reference: bool = False
    limitations: List[str] = Field(default_factory=list)

    @property
    def band_completeness(self) -> float:
        wanted = len(self.indices_available) + len(self.indices_unavailable)
        return 1.0 if not wanted else len(self.indices_available) / wanted


class LandCoverMeasurement(BaseModel):
    """Single-image land-cover measurement (drives VQA, captioning, grounding)."""

    kind: str = "land_cover"
    method: str = ""
    classes: List[ClassStat] = Field(default_factory=list)
    dominant: str = ""
    indices: List[IndexStat] = Field(default_factory=list)
    regions: List[Region] = Field(default_factory=list)
    quality: QualityReport = Field(default_factory=QualityReport)
    scene_labels: List[dict] = Field(default_factory=list)   # adapted BigEarthNet head
    label_source: Optional[str] = None

    def get(self, name: str) -> Optional[ClassStat]:
        return next((c for c in self.classes if c.name == name), None)

    def fraction(self, name: str) -> float:
        c = self.get(name)
        return c.fraction if c else 0.0


class ClassDelta(BaseModel):
    """Change in one class between two acquisitions."""

    name: str
    label: str = ""
    t1_fraction: float = 0.0
    t2_fraction: float = 0.0
    delta_fraction: float = 0.0
    delta_ha: Optional[float] = None
    relative_change: Optional[float] = None    # delta / t1_fraction
    direction: str = "unchanged"               # increased | decreased | unchanged
    significant: bool = False


class ChangeMeasurement(BaseModel):
    """Bi-temporal change measurement (drives change-VQA and change description)."""

    kind: str = "change"
    method: str = ""
    per_class: List[ClassDelta] = Field(default_factory=list)
    transitions: List[dict] = Field(default_factory=list)    # {from,to,pixels,fraction,area_ha}
    changed_fraction: float = 0.0
    changed_area_ha: Optional[float] = None
    magnitude_threshold: Optional[float] = None
    noise_floor: Optional[float] = None
    clusters: List[Region] = Field(default_factory=list)
    t1_date: Optional[str] = None
    t2_date: Optional[str] = None
    change_map_path: Optional[str] = None
    t1: Optional[LandCoverMeasurement] = None
    t2: Optional[LandCoverMeasurement] = None
    quality: QualityReport = Field(default_factory=QualityReport)

    def delta(self, name: str) -> Optional[ClassDelta]:
        return next((d for d in self.per_class if d.name == name), None)


class AgreementStat(BaseModel):
    """How far the optical and SAR evidence agree about one class."""

    class_name: str
    optical_fraction: Optional[float] = None
    sar_fraction: Optional[float] = None
    joint_fraction: float = 0.0
    iou: Optional[float] = None
    cohen_kappa: Optional[float] = None
    sar_only_fraction: float = 0.0
    optical_only_fraction: float = 0.0


class FusionMeasurement(BaseModel):
    """Optical + SAR joint extraction (drives the cross-modal specialist)."""

    kind: str = "fusion"
    method: str = ""
    joint_classes: List[ClassStat] = Field(default_factory=list)
    agreement: List[AgreementStat] = Field(default_factory=list)
    mean_agreement: Optional[float] = None
    obscured_fraction: float = 0.0             # optical cloud/haze cover recovered by SAR
    complementarity: List[str] = Field(default_factory=list)
    regions: List[Region] = Field(default_factory=list)
    optical: Optional[LandCoverMeasurement] = None
    sar: Optional[LandCoverMeasurement] = None
    overlay_path: Optional[str] = None
    quality: QualityReport = Field(default_factory=QualityReport)


class GroundingMeasurement(BaseModel):
    """Referring-expression grounding result."""

    kind: str = "grounding"
    method: str = ""
    target_class: str = ""
    target_phrase: str = ""
    qualifier: Optional[str] = None
    regions: List[Region] = Field(default_factory=list)
    candidates_considered: int = 0
    overlay_path: Optional[str] = None
    land_cover: Optional[LandCoverMeasurement] = None
    quality: QualityReport = Field(default_factory=QualityReport)


Measurement = (LandCoverMeasurement | ChangeMeasurement | FusionMeasurement
               | GroundingMeasurement)


def class_stat(name: str, pixel_count: int, total: int, area_ha: Optional[float],
               basis: str, reliability: str = "medium", region_count: int = 0) -> ClassStat:
    return ClassStat(
        name=name, label=CLASS_LABELS.get(name, name), pixel_count=int(pixel_count),
        fraction=(pixel_count / total) if total else 0.0, area_ha=area_ha,
        evidence_basis=basis, reliability=reliability, region_count=region_count)


def summarize_classes(classes: List[ClassStat], top: int = 3) -> str:
    """Human-readable extent summary, largest class first."""
    ranked = [c for c in sorted(classes, key=lambda c: -c.fraction) if c.fraction > 0.005][:top]
    if not ranked:
        return "no class exceeds 0.5% of the scene"
    parts = []
    for c in ranked:
        area = f" ({c.area_ha:.1f} ha)" if c.area_ha else ""
        parts.append(f"{CLASS_LABELS.get(c.name, c.name)} {c.fraction * 100:.1f}%{area}")
    return "; ".join(parts)
