"""Executor (Controller stage 4): sequence the validated pipeline.

Runs: validate -> classify -> registry lookup -> prepare images -> specialist
predict -> combined outputs + confidence -> audit trace persisted. Only the
predefined registry's permitted parameters reach the specialists.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from backend.config import TRACE_DIR, settings
from backend.controller import combiner
from backend.controller.audit import AuditTrace, ValidationReport, log_trace, now_ms
from backend.controller.classifier import classify
from backend.controller.registry import entry_for_task, filter_permitted_params
from backend.controller.validator import ValidationError, validate_inputs
from backend.preprocessing import pipeline as prep
from backend.specialists.tasks import specialist_for_task


def _query_hash(query: str) -> str:
    return hashlib.sha1(query.encode("utf-8")).hexdigest()[:10]


def now() -> int:
    return int(time.time() * 1000)


def run_pipeline(file_paths: List[str], query: str, run_id: str,
                 model_override: Optional[str] = None, model_params: Optional[Dict[str, object]] = None,
                 ground_truth: Optional[str] = None) -> Tuple[AuditTrace, Optional[PreparedResult]]:
    """Full controller pass. Returns (audit_trace, result) where result is None on rejection."""
    t0 = now()

    # Stage 1: validate inputs (rejects and explains on failure).
    try:
        validation: ValidationReport = validate_inputs(file_paths, query)
    except ValidationError as exc:
        trace = AuditTrace(task="rejected", query=query,
                           query_hash=_query_hash(query),
                           input_config=settings_dummy_config(file_paths),
                           validation=ValidationReport(
                               passed=[], warnings=[], failed=[str(exc)],
                               checks=[], input_config=settings_dummy_config(file_paths), accepted=False),
                           registry_entries_used=[], outputs=prep_dummy_outputs(str(exc)),
                           execution_time_ms=now_ms() - t0, model_backend="validator_gate")
        log_trace(trace, run_id)
        return trace, None

    images = [prep.prepare_image(p) for p in file_paths]
    prep.prepare_report_images(images, run_id)
    previews = [im.preview_path for im in images]

    # Stage 2: classify the natural-language query.
    classification = classify(query, validation.input_config)

    # Stage 3: predefined registry lookup.
    entry = entry_for_task(classification.task)
    if entry is None:
        raise RuntimeError(f"No registry entry for task {classification.task}")

    # Stage 4: permitted parameters only.
    params = filter_permitted_params(classification.task, model_params)

    # Stage 5: run the specialist.
    specialist = specialist_for_task(classification.task)
    result = specialist.predict(query, images, params)
    if not result.text and not result.boxes:
        raise RuntimeError(f"Specialist {classification.task} produced empty output.")

    # Stage 6: combine + confidence.
    outputs = combiner.combine(result, previews)
    confidence = combiner.build_confidence(result)
    entry_used = combiner.registry_entry_used(result, entry.id, classification.task, params)

    trace = AuditTrace(
        task=classification.task,
        query=query,
        query_hash=_query_hash(query),
        input_config=validation.input_config,
        classification=classification.to_audit(),
        validation=validation,
        registry_entries_used=[entry_used],
        execution_time_ms=now_ms() - t0,
        confidence=confidence,
        outputs=outputs,
        model_backend="mock" if result.confidence_source == "mock" else ("adapter" if result.adapter else "zero-shot"),
        server_version=settings.version,
    )
    log_trace(trace, run_id)

    reported = PreparedResult(query=query, task=classification.task, text=result.text,
                              boxes=[b.model_dump() for b in result.boxes],
                              mask_path=result.mask.path if result.mask.path else None,
                              confidence=confidence,
                              evidence=previews,
                              model_id=result.model_id, adapter=result.adapter,
                              trace_path=str(TRACE_DIR / f"{run_id}.json"))
    return trace, reported


def settings_dummy_config(file_paths: List[str]):
    from backend.controller.audit import InputConfig
    return InputConfig(n_images=len(file_paths), images=[], geolocated=False)


def prep_dummy_outputs(text: str):
    from backend.controller.audit import MaskOutput, Outputs, TextOutput
    return Outputs(text=TextOutput(text=text), mask=MaskOutput())


class PreparedResult:
    """Public result object handed to the API layer (JSON-safe fields)."""

    def __init__(self, query: str, task: str, text: str, boxes: List[dict],
                 mask_path: Optional[str], confidence, evidence: List[str],
                 model_id: str, adapter: Optional[str], trace_path: str):
        self.query = query
        self.task = task
        self.text = text
        self.boxes = boxes
        self.mask_path = mask_path
        self.confidence = confidence
        self.evidence = evidence
        self.model_id = model_id
        self.adapter = adapter
        self.trace_path = trace_path

    def to_dict(self) -> dict:
        return {"query": self.query, "task": self.task, "text": self.text,
                "boxes": self.boxes, "mask_path": self.mask_path,
                "confidence": self.confidence.model_dump() if self.confidence else None,
                "evidence": self.evidence, "model_id": self.model_id,
                "adapter": self.adapter, "trace_path": self.trace_path,
                "answers": [self.text]}


def _static_call(files: List[str], query: str) -> Tuple[AuditTrace, Optional["PreparedResult"]]:
    run_id = f"run_{int(time.time() * 1000)}"
    return run_pipeline(files, query, run_id)


ExecResult = PreparedResult