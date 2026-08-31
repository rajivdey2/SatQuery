"""Task Classifier (Controller stage 2).

Maps the natural-language query plus the validated input configuration onto one of
the six problem-statement tasks. Deliberately deterministic: CLAUDE.md section 7.2
notes that a live demo in front of judges wants reproducible routing, and the
graded artefact is the routing *decision*, not the cleverness of the mechanism.

The classifier is a scored rule set rather than an if-ladder, so the trace can show
the runner-up tasks and the evidence behind each. Feasibility gates come from the
registry: a change task cannot win on a single image, and fusion cannot win without
one optical and one SAR image, regardless of the wording.

An optional ``llm_hook`` is consulted only when the top two scores are tied, and
whichever path decided is recorded in ``classification.method``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from backend.controller.audit import InputConfig
from backend.controller.registry import required_input_matches, tasks as registry_tasks

TASKS: List[str] = ["single_vqa", "single_caption", "single_grounding",
                    "change_vqa", "change_description", "sar_optical_fusion"]

# (phrase, weight) evidence per task. Multi-word phrases score higher because they
# are far less ambiguous than a single verb.
_EVIDENCE: Dict[str, List[Tuple[str, float]]] = {
    "sar_optical_fusion": [
        ("optical and sar", 4.0), ("sar and optical", 4.0), ("optical and radar", 4.0),
        ("both images together", 3.0), ("both sensors", 3.0), ("use both", 2.5),
        ("cross-modal", 2.5), ("cross modal", 2.5), ("fuse", 2.0), ("fusion", 2.0),
        ("jointly", 1.5), ("combined", 1.2), ("combine", 1.2), ("together", 1.0),
        ("multi-sensor", 2.0), ("multisensor", 2.0), ("radar", 1.0), ("sar", 0.8),
    ],
    "change_description": [
        ("what changed", 4.0), ("what has changed", 4.0), ("describe the change", 4.0),
        ("where did the change", 4.0), ("what changes", 3.0), ("changes occurred", 3.0),
        ("change between", 2.5), ("changed between", 2.5), ("difference between", 2.0),
        ("before and after", 2.0), ("summarise the change", 3.0), ("summarize the change", 3.0),
        ("change detection", 2.0), ("over time", 1.2), ("between the two dates", 3.0),
    ],
    "change_vqa": [
        ("increased", 3.0), ("decreased", 3.0), ("remained unchanged", 3.5),
        ("remained the same", 3.0), ("has the", 1.5), ("did the", 1.5),
        ("more or less", 2.0), ("grown", 2.0), ("shrunk", 2.0), ("expanded", 2.0),
        ("reduced", 1.5), ("how much has", 2.5), ("is there more", 2.0),
        ("compared to", 1.5), ("since", 0.8), ("trend", 1.5),
    ],
    "single_grounding": [
        ("highlight", 3.5), ("localise", 3.5), ("localize", 3.5), ("locate", 3.0),
        ("where is", 3.0), ("where are", 3.0), ("point to", 2.5), ("point out", 2.5),
        ("mark the", 2.5), ("show me the", 2.0), ("bounding box", 4.0), ("boxes", 2.0),
        ("outline the", 2.5), ("referred to in the query", 3.0), ("which region", 2.0),
        ("find the", 2.0), ("segment the", 2.0), ("delineate", 2.5),
    ],
    "single_caption": [
        ("describe", 3.0), ("description", 2.5), ("caption", 3.0),
        ("what can you see", 3.0), ("what do you see", 3.0), ("scene description", 3.5),
        ("land-cover and major objects", 4.0), ("land cover and major objects", 4.0),
        ("summarise the image", 3.0), ("summarize the image", 3.0), ("overview", 2.0),
        ("what is in this image", 2.5), ("tell me about", 2.0),
    ],
    "single_vqa": [
        ("how many", 2.5), ("how much", 2.0), ("what percentage", 2.5),
        ("is there", 2.0), ("are there", 2.0), ("does the image", 2.0),
        ("what is the dominant", 2.5), ("which class", 2.0), ("what type", 1.5),
        ("identify", 1.2), ("classify", 1.5), ("area of", 1.5),
    ],
}

_INTERROGATIVE = re.compile(
    r"^\s*(what|which|how|is|are|was|were|does|do|did|has|have|can|where|when|why|who)\b", re.I)
_CHANGE_HINT = re.compile(
    r"\bchange[sd]?\b|\bdiffer(?:ence|ent)?\b|\bbefore\b|\bafter\b|\bincreas\w*|\bdecreas\w*|"
    r"\bunchanged\b|\bgrow\w*|\bgrew\b|\bshrunk\b|\bshrank\b|\bexpand\w*|\bover time\b|"
    r"\bbetween (?:the )?two\b|\bbi-?temporal\b", re.I)


@dataclass
class ClassificationResult:
    task: str
    method: str
    reasons: List[str] = field(default_factory=list)
    scores: Dict[str, float] = field(default_factory=dict)
    alternatives: Dict[str, float] = field(default_factory=dict)
    infeasible: Dict[str, str] = field(default_factory=dict)
    matched_evidence: Dict[str, List[str]] = field(default_factory=dict)

    def to_audit(self) -> dict:
        runner_up = sorted(((t, s) for t, s in self.alternatives.items() if t != self.task),
                           key=lambda kv: -kv[1])
        return {"task": self.task, "method": self.method, "reasons": self.reasons,
                "scores": {k: round(v, 3) for k, v in self.scores.items()},
                "alternatives": {k: round(v, 4) for k, v in self.alternatives.items()},
                "runner_up": ({"task": runner_up[0][0], "score": round(runner_up[0][1], 4)}
                              if runner_up else None),
                "infeasible_tasks": self.infeasible,
                "matched_evidence": self.matched_evidence,
                "candidate_tasks": list(TASKS)}


def _score_query(q: str) -> Tuple[Dict[str, float], Dict[str, List[str]]]:
    scores = {t: 0.0 for t in TASKS}
    matched: Dict[str, List[str]] = {t: [] for t in TASKS}
    for task, phrases in _EVIDENCE.items():
        for phrase, weight in phrases:
            if phrase in q:
                scores[task] += weight
                matched[task].append(phrase)
    return scores, {t: v for t, v in matched.items() if v}


def _feasibility(config: Optional[InputConfig]) -> Dict[str, str]:
    """Which tasks the input configuration rules out, and why."""
    n = config.n_images if config else 1
    signature = list(config.modality_signature) if config else []
    out: Dict[str, str] = {}
    for task in registry_tasks():
        ok, reason = required_input_matches(task, n, signature)
        if not ok:
            out[task] = reason
    return out


def _softmax(scores: Dict[str, float], temperature: float = 1.5) -> Dict[str, float]:
    import math

    items = [(t, s) for t, s in scores.items()]
    top = max((s for _, s in items), default=0.0)
    exps = {t: math.exp((s - top) / max(temperature, 1e-6)) for t, s in items}
    total = sum(exps.values()) or 1.0
    return {t: v / total for t, v in exps.items()}


def classify(query: str, input_config: Optional[InputConfig] = None,
             llm_hook: Optional[Callable[[str, List[str]], str]] = None) -> ClassificationResult:
    """Route one query + input configuration to exactly one task."""
    q = " " + re.sub(r"\s+", " ", (query or "").lower().strip()) + " "
    reasons: List[str] = []
    scores, matched = _score_query(q)
    infeasible = _feasibility(input_config)
    n = input_config.n_images if input_config else 1
    cross_modal = bool(input_config and input_config.cross_modal)
    bi_temporal = bool(input_config and input_config.bi_temporal)

    # --- structural priors from the input configuration ---------------------
    if cross_modal:
        scores["sar_optical_fusion"] += 3.0
        reasons.append("Input is a co-registered optical+SAR pair, which is the defined input for "
                       "cross-modal joint extraction.")
    if bi_temporal and n == 2:
        scores["change_description"] += 1.5
        scores["change_vqa"] += 1.5
        reasons.append("Input is a same-modality pair, the defined input for change analysis.")
    if n == 1:
        scores["single_vqa"] += 0.5

    # --- query-shape priors -------------------------------------------------
    change_wanted = bool(_CHANGE_HINT.search(q))
    if change_wanted:
        if n < 2:
            reasons.append("The query asks about change, but only one image was supplied: change "
                           "tasks are infeasible and the query is answered as single-image VQA.")
            scores["single_vqa"] += 2.0
        else:
            # "what changed / where" is a description; "has X increased" is a question.
            if re.search(r"\bwhat\b.*\bchang|\bwhere\b|\bdescribe\b", q):
                scores["change_description"] += 2.0
            if re.search(r"\b(has|have|did|is|are|was|were)\b", q) and re.search(
                    r"increas|decreas|unchanged|same|more|less|grow|grew|shrunk|shrank|expand", q):
                scores["change_vqa"] += 2.5
    if _INTERROGATIVE.match(q.strip()) and not change_wanted and n == 1:
        scores["single_vqa"] += 1.0
        reasons.append("Interrogative single-image query.")

    # --- apply feasibility gates -------------------------------------------
    for task, reason in infeasible.items():
        if scores.get(task, 0.0) > 0:
            reasons.append(f"'{task}' scored on wording but is infeasible: {reason}.")
        scores[task] = float("-inf")

    feasible = {t: s for t, s in scores.items() if s != float("-inf")}
    if not feasible:
        reasons.append("No registry entry accepts this input configuration.")
        return ClassificationResult(task="rejected", method="rules", reasons=reasons,
                                    scores={}, alternatives={}, infeasible=infeasible,
                                    matched_evidence=matched)

    ranked = sorted(feasible.items(), key=lambda kv: -kv[1])
    best, best_score = ranked[0]
    method = "rules"

    if len(ranked) > 1 and abs(ranked[0][1] - ranked[1][1]) < 1e-9:
        tie = [t for t, s in ranked if abs(s - best_score) < 1e-9]
        if llm_hook is not None:
            try:
                choice = llm_hook(query, tie)
            except Exception as exc:  # pragma: no cover
                choice = None
                reasons.append(f"LLM tie-break hook failed ({type(exc).__name__}); used the "
                               "deterministic order.")
            if choice in tie:
                best, method = choice, "rules+llm_tiebreak"
                reasons.append(f"Scores tied across {tie}; the LLM tie-break selected '{best}'.")
        if method == "rules":
            best = _deterministic_tiebreak(tie, n, cross_modal, bi_temporal)
            reasons.append(f"Scores tied across {tie}; deterministic fallback selected '{best}'.")

    if best_score <= 0.0:
        default = ("sar_optical_fusion" if cross_modal else
                   "change_description" if (bi_temporal and n == 2) else "single_vqa")
        if default in feasible:
            best = default
            reasons.append(f"No task keyword matched; defaulted to '{best}' for this input "
                           "configuration.")

    if matched.get(best):
        reasons.append(f"Matched {best} evidence: {', '.join(matched[best][:4])}.")

    return ClassificationResult(
        task=best, method=method, reasons=reasons,
        scores={t: (s if s != float("-inf") else -1.0) for t, s in scores.items()},
        alternatives=_softmax(feasible), infeasible=infeasible, matched_evidence=matched)


def _deterministic_tiebreak(tie: List[str], n: int, cross_modal: bool, bi_temporal: bool) -> str:
    priority = (["sar_optical_fusion", "change_description", "change_vqa"] if cross_modal else
                ["change_description", "change_vqa", "sar_optical_fusion"] if bi_temporal else
                ["single_grounding", "single_caption", "single_vqa"])
    for task in priority:
        if task in tie:
            return task
    return sorted(tie)[0]
