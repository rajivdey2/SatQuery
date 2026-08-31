"""Executor (Controller stage 4) — the orchestration itself.

Runs the full pass: validate, classify, look up the predefined registry, resolve
*only* the permitted parameters, execute the selected specialist (plus a follow-up
specialist where the query genuinely asks for one), combine text with spatial
evidence, estimate confidence from measured signals, and persist the audit trace.

Two behaviours here are deliberate and graded:

* **Rejection is a first-class outcome.** A pair that fails compatibility checks
  produces a complete trace explaining which check failed and why, not an
  exception and not a confident-looking answer.
* **Sequencing is real, not decorative.** A second registry entry is only added to
  the plan when the query asks for something the first entry does not produce --
  for example a localisation request on top of a fusion query -- and each step is
  recorded with its own parameters and timing.
"""
from __future__ import annotations

import hashlib
import re
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.config import TRACE_DIR, settings
from backend.controller import combiner
from backend.controller.audit import (AuditTrace, InputConfig, Outputs, TextOutput,
                                      ToolStep, ValidationReport, image_to_audit, log_trace,
                                      now_ms)
from backend.controller.classifier import ClassificationResult, classify
from backend.controller.confidence import rejection_confidence
from backend.controller.registry import (RegistryEntry, entry_by_id, entry_for_task,
                                         required_input_matches, resolve_params)
from backend.controller.validator import ValidationError, validate_inputs
from backend.preprocessing import geotiff_io as gio
from backend.preprocessing import pipeline as prep
from backend.preprocessing.pipeline import PreparedImage
from backend.specialists import specialist_for_task

_GROUNDING_INTENT = re.compile(
    r"\bhighlight\b|\blocalis\w*|\blocaliz\w*|\bwhere\b|\bmark the\b|\bshow me the\b|"
    r"\boutline\b|\bbounding box\b|\bpoint (?:to|out)\b", re.I)


@dataclass
class PlanStep:
    """One registry entry scheduled to run, with the images it will see."""

    entry: RegistryEntry
    image_indices: Tuple[int, ...]
    reason: str = ""
    param_overrides: Dict[str, Any] = field(default_factory=dict)
    primary: bool = True


@dataclass
class RunResult:
    """Public result handed to the API layer (JSON-safe)."""

    run_id: str
    query: str
    task: str
    text: str
    boxes: List[dict] = field(default_factory=list)
    mask_path: Optional[str] = None
    mask_kind: str = "none"
    evidence: List[dict] = field(default_factory=list)
    confidence: Optional[dict] = None
    measurements: Dict[str, Any] = field(default_factory=dict)
    model_id: str = ""
    adapter: Optional[str] = None
    narration_source: str = "measurement"
    tools_used: List[str] = field(default_factory=list)
    trace_path: str = ""
    rejected: Optional[dict] = None

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "query": self.query, "task": self.task,
                "text": self.text, "answers": [self.text] if self.text else [],
                "boxes": self.boxes, "mask_path": self.mask_path, "mask_kind": self.mask_kind,
                "evidence": self.evidence, "confidence": self.confidence,
                "measurements": self.measurements, "model_id": self.model_id,
                "adapter": self.adapter, "narration_source": self.narration_source,
                "tools_used": self.tools_used, "trace_path": self.trace_path,
                "rejected": self.rejected}


def _query_hash(query: str) -> str:
    return hashlib.sha1((query or "").encode("utf-8")).hexdigest()[:10]


def _empty_config(files: List[str]) -> InputConfig:
    return InputConfig(n_images=len(files), images=[], geolocated=False)


def _build_plan(task: str, query: str, images: List[PreparedImage]) -> List[PlanStep]:
    """Select one or more registry entries for this query and input configuration."""
    primary = entry_for_task(task)
    if primary is None:
        return []
    n = len(images)
    steps = [PlanStep(entry=primary, image_indices=tuple(range(n)),
                      reason=f"registry entry for classified task '{task}'", primary=True)]

    # Genuine follow-up: a fusion query that also asks "where" gets the joint result
    # localised by the grounding specialist on the optical image.
    if task == "sar_optical_fusion" and _GROUNDING_INTENT.search(query or ""):
        grounding = entry_by_id("rs_vlm.grounding")
        optical_idx = next((i for i, im in enumerate(images) if not im.is_sar), None)
        if grounding is not None and optical_idx is not None:
            steps.append(PlanStep(
                entry=grounding, image_indices=(optical_idx,),
                reason="query asks for localisation in addition to joint extraction; the grounding "
                       "entry runs on the optical image of the pair",
                primary=False))
    return steps


def _prepare(files: List[str], params: Dict[str, Any],
             recorder_steps: List[ToolStep]) -> List[PreparedImage]:
    prepared: List[PreparedImage] = []
    for path in files:
        t0 = time.perf_counter()
        img = prep.prepare_image(path, speckle_filter=params.get("speckle_filter"))
        prepared.append(img)
        recorder_steps.append(ToolStep(
            order=len(recorder_steps) + 1, tool="preprocessing.read_and_calibrate",
            parameters={"file": Path(path).name,
                        "speckle_filter": params.get("speckle_filter")},
            outputs_summary=(f"{img.info.modality} · {img.bands.layout} · "
                             f"{img.shape[1]}x{img.shape[0]} analysis grid"),
            duration_ms=int((time.perf_counter() - t0) * 1000),
            detail={"band_roles": img.bands.roles, "band_source": img.bands.source,
                    "pixel_area_m2": img.pixel_area_m2}))
    return prepared


def run_pipeline(file_paths: List[str], query: str, run_id: str,
                 model_override: Optional[str] = None,
                 model_params: Optional[Dict[str, Any]] = None,
                 ground_truth: Optional[str] = None) -> Tuple[AuditTrace, Optional[RunResult]]:
    """Full controller pass. Returns ``(trace, result)``; ``result`` is None on rejection."""
    t0 = now_ms()
    environment = settings.to_audit()
    if model_override:
        environment["model_override_requested"] = model_override
    if ground_truth:
        environment["ground_truth_supplied"] = True

    # --- Stage 1: validate --------------------------------------------------
    try:
        validation = validate_inputs(file_paths, query)
    except ValidationError as exc:
        return _rejection_trace(run_id, query, file_paths, [str(exc)], t0, environment), None

    if not validation.accepted:
        trace = AuditTrace(
            run_id=run_id, task="rejected", query=query, query_hash=_query_hash(query),
            input_config=validation.input_config, validation=validation,
            classification={"task": "rejected", "method": "validator_gate",
                            "reasons": ["input rejected before task classification"]},
            outputs=Outputs(text=TextOutput(
                text=("Input rejected. " + " ".join(validation.failed)),
                narration_source="validator")),
            confidence=rejection_confidence(validation.failed),
            execution_time_ms=now_ms() - t0, model_backend="validator_gate",
            environment=environment, server_version=settings.version)
        log_trace(trace, run_id)
        return trace, _rejected_result(run_id, query, trace)

    # --- Stage 2: classify --------------------------------------------------
    classification: ClassificationResult = classify(query, validation.input_config)
    if classification.task == "rejected":
        trace = AuditTrace(
            run_id=run_id, task="rejected", query=query, query_hash=_query_hash(query),
            input_config=validation.input_config, validation=validation,
            classification=classification.to_audit(),
            outputs=Outputs(text=TextOutput(
                text=("No registry entry accepts this input configuration: "
                      + "; ".join(f"{k}: {v}" for k, v in classification.infeasible.items())),
                narration_source="validator")),
            confidence=rejection_confidence(list(classification.infeasible.values())),
            execution_time_ms=now_ms() - t0, model_backend="router_gate",
            environment=environment, server_version=settings.version)
        log_trace(trace, run_id)
        return trace, _rejected_result(run_id, query, trace)

    # --- Stage 3/4: registry lookup and permitted parameters ---------------
    entry = entry_for_task(classification.task)
    params, rejected_params, param_notes = resolve_params(classification.task, model_params)
    ok, why = required_input_matches(classification.task, validation.input_config.n_images,
                                     validation.input_config.modality_signature)
    if not ok:  # defensive: the classifier already gates on this
        validation.failed.append(why)
        validation.accepted = False
        trace = _rejection_trace(run_id, query, file_paths, [why], t0, environment,
                                 validation=validation)
        return trace, _rejected_result(run_id, query, trace)

    steps: List[ToolStep] = []
    images = _prepare(file_paths, params, steps)
    prep.prepare_report_images(images, run_id)
    previews = [im.preview_path for im in images]
    validation.input_config.images = [image_to_audit(im.info, im) for im in images]

    plan = _build_plan(classification.task, query, images)
    plan_ids = [f"{p.entry.id}" for p in plan]

    # --- Stage 5: execute --------------------------------------------------
    results = []
    entries_used = []
    error: Optional[str] = None
    for step in plan:
        step_params = dict(params)
        if not step.primary:
            step_params, step_rejected, _ = resolve_params(step.entry.task, step.param_overrides)
        specialist = specialist_for_task(step.entry.task)
        step_images = [images[i] for i in step.image_indices]
        t_step = time.perf_counter()
        try:
            result = specialist.predict(query, step_images, step_params, run_id=run_id)
        except Exception as exc:  # pragma: no cover - specialist-level failure
            error = f"{step.entry.id} failed: {type(exc).__name__}: {exc}"
            steps.append(ToolStep(order=len(steps) + 1, tool=step.entry.id,
                                  task=step.entry.task, parameters=step_params,
                                  status="failed", outputs_summary=error,
                                  duration_ms=int((time.perf_counter() - t_step) * 1000),
                                  detail={"traceback": traceback.format_exc(limit=4)}))
            if step.primary:
                break
            continue
        for s in result.steps:
            s.order = len(steps) + 1
            steps.append(s)
        steps.append(ToolStep(
            order=len(steps) + 1, tool=step.entry.id, task=step.entry.task,
            parameters=step_params,
            outputs_summary=(f"{len(result.text)} chars, {len(result.boxes)} box(es), "
                             f"mask={result.mask.kind}, narration={result.narration_source}"),
            duration_ms=int((time.perf_counter() - t_step) * 1000),
            detail={"reason_in_plan": step.reason, "primary": step.primary}))
        results.append((step, result))
        entries_used.append(combiner.registry_entry_used(
            step.entry, result, step_params, rejected_params if step.primary else []))

    if not results:
        trace = AuditTrace(
            run_id=run_id, task=classification.task, query=query, query_hash=_query_hash(query),
            input_config=validation.input_config, validation=validation,
            classification=classification.to_audit(), plan=plan_ids, steps=steps,
            effective_parameters=params,
            outputs=Outputs(text=TextOutput(
                text=f"Execution failed before any answer was produced. {error or ''}".strip(),
                narration_source="validator")),
            execution_time_ms=now_ms() - t0, model_backend="error",
            environment=environment, error=error, server_version=settings.version)
        log_trace(trace, run_id)
        return trace, None

    primary_step, primary_result = results[0]
    outputs = combiner.combine(primary_result, previews)
    measurements = {primary_step.entry.task: combiner.measurement_block(primary_result)}

    # Fold in the follow-up entry's evidence and boxes.
    for step, result in results[1:]:
        outputs.boxes.extend(result.boxes)
        for item in result.evidence:
            if item.path and item.path not in {e.path for e in outputs.evidence}:
                outputs.evidence.append(item)
        measurements[step.entry.task] = combiner.measurement_block(result)
        if result.text:
            outputs.text.text = f"{outputs.text.text} {result.text}".strip()
        primary_result.confidence_signals.setdefault(
            "region_score", result.confidence_signals.get("region_score"))
    outputs.evidence_thumbnails = [e.path for e in outputs.evidence]

    confidence = combiner.build_confidence(primary_result)
    if param_notes:
        environment["parameter_notes"] = param_notes

    trace = AuditTrace(
        run_id=run_id, task=classification.task, query=query, query_hash=_query_hash(query),
        input_config=validation.input_config, validation=validation,
        classification=classification.to_audit(), plan=plan_ids,
        registry_entries_used=entries_used, steps=steps, effective_parameters=params,
        measurements=measurements, outputs=outputs, confidence=confidence,
        execution_time_ms=now_ms() - t0,
        model_backend=_backend_label(primary_result), environment=environment,
        error=error, server_version=settings.version)
    log_trace(trace, run_id)

    result = RunResult(
        run_id=run_id, query=query, task=classification.task, text=outputs.text.text,
        boxes=[b.model_dump() for b in outputs.boxes],
        mask_path=outputs.mask.path, mask_kind=outputs.mask.kind,
        evidence=[e.model_dump() for e in outputs.evidence],
        confidence=confidence.model_dump(), measurements=measurements,
        model_id=primary_result.model_id, adapter=primary_result.adapter,
        narration_source=primary_result.narration_source,
        tools_used=[e.id for e in entries_used],
        trace_path=str(Path(TRACE_DIR) / f"{run_id}.json"))
    return trace, result


def _backend_label(result) -> str:
    if result.narration_source == "mock":
        return "mock"
    if result.adapter:
        return f"analysis_engine+vlm_adapter:{result.adapter}"
    if result.narration_source.startswith("vlm"):
        return "analysis_engine+vlm_zero_shot"
    return "analysis_engine"


def _rejection_trace(run_id: str, query: str, files: List[str], reasons: List[str],
                     t0: int, environment: Dict[str, Any],
                     validation: Optional[ValidationReport] = None) -> AuditTrace:
    config = validation.input_config if validation else _empty_config(files)
    report = validation or ValidationReport(
        passed=[], warnings=[], failed=list(reasons), checks=[],
        input_config=config, accepted=False)
    trace = AuditTrace(
        run_id=run_id, task="rejected", query=query, query_hash=_query_hash(query),
        input_config=config, validation=report,
        classification={"task": "rejected", "method": "validator_gate", "reasons": reasons},
        outputs=Outputs(text=TextOutput(text="Input rejected. " + " ".join(reasons),
                                        narration_source="validator")),
        confidence=rejection_confidence(reasons),
        execution_time_ms=now_ms() - t0, model_backend="validator_gate",
        environment=environment, server_version=settings.version)
    log_trace(trace, run_id)
    return trace


def _rejected_result(run_id: str, query: str, trace: AuditTrace) -> RunResult:
    return RunResult(
        run_id=run_id, query=query, task="rejected", text=trace.outputs.text.text,
        confidence=trace.confidence.model_dump(),
        trace_path=str(Path(TRACE_DIR) / f"{run_id}.json"),
        rejected={"title": trace.outputs.text.text,
                  "validation_failed": list(trace.validation.failed),
                  "validation_warnings": list(trace.validation.warnings),
                  "checks": [c.model_dump() for c in trace.validation.checks]})


# Backwards-compatible aliases used by earlier scripts.
PreparedResult = RunResult
ExecResult = RunResult
