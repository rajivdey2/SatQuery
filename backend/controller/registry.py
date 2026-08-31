"""The predefined model/tool registry (Controller stage 3).

The problem statement requires the controller to "select one or more models or
tools **from a predefined registry**" and to "configure only permitted task
parameters". This module is that registry, taken literally: every task declares
the tool that serves it, the input configuration it requires, and a typed,
range-checked parameter schema. Anything a caller passes that is not in the
schema is *rejected and listed in the audit trace* rather than quietly dropped or
quietly forwarded.

Nothing outside this module may decide which specialist runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.config import settings

SINGLE_IMAGE = "single_image"
BI_TEMPORAL_PAIR = "bi_temporal_pair"
OPTICAL_SAR_PAIR = "optical_sar_pair"


@dataclass
class ParamSpec:
    """One permitted task parameter, with the range the executor enforces."""

    name: str
    kind: str                                  # int | float | bool | str | enum
    default: Any = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: Tuple[Any, ...] = ()
    description: str = ""

    def coerce(self, value: Any) -> Tuple[bool, Any, str]:
        """Return ``(ok, coerced_value, reason_if_rejected)``."""
        try:
            if self.kind == "int":
                v = int(value)
            elif self.kind == "float":
                v = float(value)
            elif self.kind == "bool":
                if isinstance(value, str):
                    v = value.strip().lower() in {"1", "true", "yes", "on"}
                else:
                    v = bool(value)
            elif self.kind == "enum":
                v = value
                if v not in self.choices:
                    return False, None, f"must be one of {list(self.choices)}"
            else:
                v = str(value)
        except (TypeError, ValueError):
            return False, None, f"not coercible to {self.kind}"
        if self.minimum is not None and isinstance(v, (int, float)) and v < self.minimum:
            return False, None, f"below the permitted minimum {self.minimum}"
        if self.maximum is not None and isinstance(v, (int, float)) and v > self.maximum:
            return False, None, f"above the permitted maximum {self.maximum}"
        return True, v, ""

    def to_audit(self) -> dict:
        out = {"type": self.kind, "default": self.default, "description": self.description}
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.choices:
            out["choices"] = list(self.choices)
        return out


def _common_params() -> Dict[str, ParamSpec]:
    return {
        "min_region_pixels": ParamSpec(
            "min_region_pixels", "int", settings.min_region_pixels, 4, 100_000,
            description="Smallest connected region reported as a distinct region."),
        "smoothing_window": ParamSpec(
            "smoothing_window", "int", settings.smoothing_window, 1, 15,
            description="Morphological / majority-vote window used to clean the class map."),
        "max_new_tokens": ParamSpec(
            "max_new_tokens", "int", settings.max_new_tokens, 16, 4096,
            description="Generation budget for the optional VLM narration stage."),
        "temperature": ParamSpec(
            "temperature", "float", settings.temperature, 0.0, 1.5,
            description="Sampling temperature for the optional VLM narration stage."),
    }


def _with(**extra: ParamSpec) -> Dict[str, ParamSpec]:
    params = _common_params()
    params.update(extra)
    return params


@dataclass
class RegistryEntry:
    id: str
    task: str
    specialist: str
    required_inputs: str
    permitted_params: Dict[str, ParamSpec] = field(default_factory=dict)
    description: str = ""
    outputs: Tuple[str, ...] = ()
    tools: Tuple[str, ...] = ()
    adaptation: str = ""

    def to_audit(self) -> dict:
        return {"id": self.id, "task": self.task, "specialist": self.specialist,
                "required_inputs": self.required_inputs, "description": self.description,
                "outputs": list(self.outputs), "tool_chain": list(self.tools),
                "adaptation": self.adaptation,
                "permitted_parameters": {k: v.to_audit() for k, v in self.permitted_params.items()}}


_ADAPT_SINGLE = ("Adapted BigEarthNet-MM land-cover head (training/adapt_ben_mm.py) plus optional "
                 "RSVQAxBEN -> VRSBench LoRA adapter for the narration model (ADAPTER_RS_VLM).")
_ADAPT_CHANGE = ("Optional CDVQA + LEVIR-CC LoRA adapter for the narration model (ADAPTER_CHANGE); "
                 "the change measurement itself is model-free and sensor-agnostic.")
_ADAPT_FUSION = ("Adapted BigEarthNet-MM dual-branch (Sentinel-1 + Sentinel-2) head, plus optional "
                 "BigEarthNet-MM LoRA adapter for narration (ADAPTER_FUSION).")

REGISTRY: List[RegistryEntry] = [
    RegistryEntry(
        id="rs_vlm.vqa", task="single_vqa", specialist="rs_vlm",
        required_inputs=SINGLE_IMAGE,
        permitted_params=_with(),
        description="Single-image visual question answering over measured land cover "
                    "(mandatory single-image baseline).",
        outputs=("text", "class_map", "confidence"),
        tools=("preprocessing.prepare_image", "analysis.land_cover", "render.class_map",
               "narration.measurement|narration.vlm"),
        adaptation=_ADAPT_SINGLE),
    RegistryEntry(
        id="rs_vlm.caption", task="single_caption", specialist="rs_vlm",
        required_inputs=SINGLE_IMAGE,
        permitted_params=_with(),
        description="Scene description: dominant land cover, measured class extents and "
                    "notable regions.",
        outputs=("text", "class_map", "confidence"),
        tools=("preprocessing.prepare_image", "analysis.land_cover", "render.class_map",
               "narration.measurement|narration.vlm"),
        adaptation=_ADAPT_SINGLE),
    RegistryEntry(
        id="rs_vlm.grounding", task="single_grounding", specialist="rs_vlm",
        required_inputs=SINGLE_IMAGE,
        permitted_params=_with(
            target_class=ParamSpec("target_class", "enum", None,
                                   choices=("water", "vegetation", "built_up", "bare_soil"),
                                   description="Override phrase parsing and ground this class directly."),
            max_regions=ParamSpec("max_regions", "int", 5, 1, 25,
                                  description="Maximum number of regions returned as boxes.")),
        description="Text-guided region grounding: referring expression -> connected regions -> "
                    "boxes in 0..100 normalised coordinates (VRSBench convention).",
        outputs=("text", "boxes", "grounding_overlay", "confidence"),
        tools=("preprocessing.prepare_image", "analysis.land_cover", "analysis.grounding",
               "render.grounding_overlay", "narration.measurement|narration.vlm"),
        adaptation=_ADAPT_SINGLE),
    RegistryEntry(
        id="change.vqa", task="change_vqa", specialist="change",
        required_inputs=BI_TEMPORAL_PAIR,
        permitted_params=_with(
            change_threshold=ParamSpec("change_threshold", "float", None, 0.0, 10.0,
                                       description="Override the change-vector magnitude threshold."),
            significance=ParamSpec("significance", "float", settings.change_significance, 0.0, 0.5,
                                   description="Minimum class-fraction delta called a real change."),
            normalize_radiometry=ParamSpec("normalize_radiometry", "bool", True,
                                           description="Match the later scene's radiometry to the earlier one.")),
        description="Change-based VQA over a bi-temporal pair, with per-class area deltas and a "
                    "significance test (mandatory multi-image task).",
        outputs=("text", "change_map", "boxes", "confidence"),
        tools=("preprocessing.pair_alignment", "analysis.change_detection", "render.change_map",
               "narration.measurement|narration.vlm"),
        adaptation=_ADAPT_CHANGE),
    RegistryEntry(
        id="change.description", task="change_description", specialist="change",
        required_inputs=BI_TEMPORAL_PAIR,
        permitted_params=_with(
            change_threshold=ParamSpec("change_threshold", "float", None, 0.0, 10.0,
                                       description="Override the change-vector magnitude threshold."),
            significance=ParamSpec("significance", "float", settings.change_significance, 0.0, 0.5,
                                   description="Minimum class-fraction delta called a real change."),
            normalize_radiometry=ParamSpec("normalize_radiometry", "bool", True,
                                           description="Match the later scene's radiometry to the earlier one.")),
        description="Change description: what changed, by how much, and where, with a rendered "
                    "spatial change map.",
        outputs=("text", "change_map", "transitions", "confidence"),
        tools=("preprocessing.pair_alignment", "analysis.change_detection", "render.change_map",
               "narration.measurement|narration.vlm"),
        adaptation=_ADAPT_CHANGE),
    RegistryEntry(
        id="fusion.joint", task="sar_optical_fusion", specialist="fusion",
        required_inputs=OPTICAL_SAR_PAIR,
        permitted_params=_with(
            regions_per_class=ParamSpec("regions_per_class", "int", 4, 1, 20,
                                        description="Regions reported per joint class."),
            speckle_filter=ParamSpec("speckle_filter", "bool", settings.speckle_filter,
                                     description="Apply the refined-Lee speckle filter to SAR channels.")),
        description="Optical+SAR joint extraction with per-class cross-modal agreement (Cohen's "
                    "kappa / IoU) and obscured-area recovery (mandatory cross-modal task).",
        outputs=("text", "fusion_map", "agreement", "boxes", "confidence"),
        tools=("preprocessing.modality_split", "analysis.optical_sar_fusion", "render.fusion_map",
               "narration.measurement|narration.vlm"),
        adaptation=_ADAPT_FUSION),
]

_BY_TASK = {e.task: e for e in REGISTRY}
_BY_ID = {e.id: e for e in REGISTRY}


def entry_for_task(task: str) -> Optional[RegistryEntry]:
    return _BY_TASK.get(task)


def entry_by_id(entry_id: str) -> Optional[RegistryEntry]:
    return _BY_ID.get(entry_id)


def tasks() -> List[str]:
    return [e.task for e in REGISTRY]


def resolve_params(task: str, requested: Optional[Dict[str, Any]] = None
                   ) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Build the effective parameter set for a task.

    Returns ``(effective, rejected, notes)``. ``rejected`` names every parameter
    the caller supplied that the registry does not permit for this task, or whose
    value fell outside the declared range -- both appear in the audit trace.
    """
    entry = entry_for_task(task)
    if entry is None:
        return {}, list((requested or {}).keys()), [f"unknown task '{task}'"]
    effective = {name: spec.default for name, spec in entry.permitted_params.items()}
    rejected: List[str] = []
    notes: List[str] = []
    for key, value in (requested or {}).items():
        spec = entry.permitted_params.get(key)
        if spec is None:
            rejected.append(key)
            notes.append(f"'{key}' is not a permitted parameter for {task}; ignored.")
            continue
        if value is None:
            continue
        ok, coerced, reason = spec.coerce(value)
        if not ok:
            rejected.append(key)
            notes.append(f"'{key}'={value!r} rejected: {reason}.")
            continue
        effective[key] = coerced
        notes.append(f"'{key}' set to {coerced!r} by request (permitted).")
    return effective, rejected, notes


def required_input_matches(task: str, n_images: int, modality_signature: List[str]) -> Tuple[bool, str]:
    """Check the input configuration against the entry's declared requirement."""
    entry = entry_for_task(task)
    if entry is None:
        return False, f"no registry entry for task '{task}'"
    need = entry.required_inputs
    sar = "sar" in modality_signature
    optical = any(m in ("optical", "multispectral", "panchromatic") for m in modality_signature)
    if need == SINGLE_IMAGE:
        if n_images != 1:
            return False, f"{entry.id} requires exactly one image, {n_images} supplied"
        return True, ""
    if n_images != 2:
        return False, f"{entry.id} requires an image pair, {n_images} supplied"
    if need == OPTICAL_SAR_PAIR and not (sar and optical):
        return False, (f"{entry.id} requires one optical and one SAR image; "
                       f"modalities supplied: {modality_signature}")
    if need == BI_TEMPORAL_PAIR and sar and optical:
        return False, (f"{entry.id} requires two acquisitions of the same modality; "
                       f"an optical+SAR pair was supplied")
    return True, ""


def describe_registry() -> dict:
    """Full registry description, served at /api/registry and shown in the GUI."""
    from backend.analysis import scene_labels
    from backend.specialists.backends import model_loader

    return {"entries": [e.to_audit() for e in REGISTRY],
            "input_configurations": {
                SINGLE_IMAGE: "one optical/multispectral or SAR image",
                BI_TEMPORAL_PAIR: "two spatially corresponding images of the same area, different dates",
                OPTICAL_SAR_PAIR: "co-registered optical/multispectral + SAR pair of the same area"},
            "narration_backend": model_loader.describe(),
            "adapted_head": scene_labels.describe()}
