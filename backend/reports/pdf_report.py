"""PDF report generation.

"Downloadable reports" is an explicit expectation of the problem statement, and the
report is where the graded artefacts come together on one page: the query, the
answer, the confidence with its individual signals, the measured numbers, the
executed tool sequence with parameters, every input check, the rendered visual
evidence, and the raw audit trace.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle, XPreformatted)

from backend.config import OUTPUT_DIR

_ACCENT = colors.HexColor("#1f6feb")
_MUTED = colors.HexColor("#57606a")
_GRID = colors.HexColor("#d0d7de")
_HEAD_BG = colors.HexColor("#f2f5f9")
_OK = colors.HexColor("#1a7f37")
_WARN = colors.HexColor("#9a6700")
_FAIL = colors.HexColor("#cf222e")

_STATUS_COLOR = {"passed": _OK, "warning": _WARN, "failed": _FAIL,
                 "ok": _OK, "skipped": _WARN}


def _styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontSize=19, spaceAfter=2),
        "subtitle": ParagraphStyle("st", parent=base["Normal"], fontSize=8.5, textColor=_MUTED,
                                   spaceAfter=10),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontSize=12, textColor=_ACCENT,
                             spaceBefore=12, spaceAfter=4),
        "body": ParagraphStyle("b", parent=base["BodyText"], fontSize=9, leading=13,
                               alignment=TA_LEFT),
        "answer": ParagraphStyle("a", parent=base["BodyText"], fontSize=10.5, leading=15),
        "small": ParagraphStyle("s", parent=base["BodyText"], fontSize=7.5, leading=10,
                                textColor=_MUTED),
        "cell": ParagraphStyle("c", parent=base["BodyText"], fontSize=7.5, leading=9.5),
        "mono": ParagraphStyle("m", parent=base["BodyText"], fontName="Courier", fontSize=6,
                               leading=7.4),
    }


def _table(rows: List[List[Any]], widths: List[float], styles, header: bool = True) -> Table:
    data = [[Paragraph(str(c), styles["cell"]) if not isinstance(c, Paragraph) else c
             for c in row] for row in rows]
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [("GRID", (0, 0), (-1, -1), 0.4, _GRID),
             ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("LEFTPADDING", (0, 0), (-1, -1), 4),
             ("RIGHTPADDING", (0, 0), (-1, -1), 4),
             ("TOPPADDING", (0, 0), (-1, -1), 3),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), _HEAD_BG),
                  ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
    t.setStyle(TableStyle(style))
    return t


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, (list, tuple)):
        return ", ".join(_fmt(x) for x in v) or "—"
    if isinstance(v, dict):
        return ", ".join(f"{k}={_fmt(x)}" for k, x in list(v.items())[:6]) or "—"
    return str(v)


def _pct(v: Any) -> str:
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _confidence_section(conf: dict, styles) -> List[Any]:
    if not conf:
        return []
    value = conf.get("value")
    head = (f"<b>{value:.2f}</b> · source <b>{conf.get('source', 'not_available')}</b>"
            if isinstance(value, (int, float)) else
            f"<b>not available</b> · source {conf.get('source', 'not_available')}")
    head += f" · calibrated: {'yes' if conf.get('calibrated') else 'no'}"
    out: List[Any] = [Paragraph("Confidence", styles["h2"]), Paragraph(head, styles["body"])]
    components = conf.get("components") or []
    if components:
        rows = [["signal", "value", "weight", "what it measures"]]
        rows += [[c.get("name", ""), _fmt(c.get("value")), _fmt(c.get("weight")), c.get("note", "")]
                 for c in components]
        out.append(Spacer(1, 4))
        out.append(_table(rows, [3.4 * cm, 1.7 * cm, 1.6 * cm, 10.3 * cm], styles))
    if conf.get("note"):
        out.append(Spacer(1, 3))
        out.append(Paragraph(conf["note"], styles["small"]))
    return out


def _measurement_sections(measurements: Dict[str, Any], styles) -> List[Any]:
    out: List[Any] = []
    for task, block in (measurements or {}).items():
        if not isinstance(block, dict) or not block:
            continue
        out.append(Paragraph(f"Measurements — {task} ({block.get('method', 'n/a')})", styles["h2"]))

        classes = block.get("classes") or block.get("joint_classes")
        if classes:
            rows = [["class", "extent", "area (ha)", "regions", "reliability", "evidence basis"]]
            for c in classes:
                if not c.get("pixel_count") and not c.get("fraction"):
                    continue
                rows.append([c.get("label") or c.get("name", ""), _pct(c.get("fraction")),
                             _fmt(c.get("area_ha")), _fmt(c.get("region_count")),
                             c.get("reliability", ""), c.get("evidence_basis", "")])
            if len(rows) > 1:
                out.append(_table(rows, [3.0 * cm, 1.5 * cm, 1.7 * cm, 1.3 * cm, 1.8 * cm, 7.7 * cm],
                                  styles))

        per_class = block.get("per_class")
        if per_class:
            rows = [["class", "t1", "t2", "delta", "delta (ha)", "verdict", "significant"]]
            for d in per_class:
                rows.append([d.get("label") or d.get("name", ""), _pct(d.get("t1_fraction")),
                             _pct(d.get("t2_fraction")),
                             f"{(d.get('delta_fraction') or 0) * 100:+.2f} pp",
                             _fmt(d.get("delta_ha")), d.get("direction", ""),
                             "yes" if d.get("significant") else "no"])
            out.append(Spacer(1, 4))
            out.append(_table(rows, [3.4 * cm, 1.8 * cm, 1.8 * cm, 2.2 * cm, 2.2 * cm,
                                     2.4 * cm, 2.2 * cm], styles))
            changed_ha = block.get("changed_area_ha")
            area_txt = f" ({changed_ha} ha)" if changed_ha else ""
            out.append(Paragraph(
                f"Changed area {_pct(block.get('changed_fraction'))}{area_txt} · change threshold "
                f"{_fmt(block.get('magnitude_threshold'))} against an estimated noise floor of "
                f"{_fmt(block.get('noise_floor'))}.", styles["small"]))

        transitions = block.get("transitions")
        if transitions:
            rows = [["from", "to", "extent", "area (ha)", "location"]]
            rows += [[t.get("from", ""), t.get("to", ""), _pct(t.get("fraction")),
                      _fmt(t.get("area_ha")), t.get("location", "")] for t in transitions[:8]]
            out.append(Spacer(1, 4))
            out.append(_table(rows, [3.2 * cm, 3.2 * cm, 2.0 * cm, 2.2 * cm, 6.4 * cm], styles))

        agreement = block.get("agreement")
        if agreement:
            rows = [["class", "optical", "SAR", "joint", "IoU", "Cohen kappa", "SAR-only"]]
            rows += [[a.get("class_name", ""), _pct(a.get("optical_fraction")),
                      _pct(a.get("sar_fraction")), _pct(a.get("joint_fraction")),
                      _fmt(a.get("iou")), _fmt(a.get("cohen_kappa")),
                      _pct(a.get("sar_only_fraction"))] for a in agreement]
            out.append(Spacer(1, 4))
            out.append(_table(rows, [3.0 * cm, 2.1 * cm, 2.1 * cm, 2.1 * cm, 1.8 * cm,
                                     2.5 * cm, 2.4 * cm], styles))

        regions = block.get("regions") or block.get("clusters")
        if regions:
            rows = [["label", "box [x1,y1,x2,y2] 0-100", "position", "area (ha)", "extent", "score"]]
            rows += [[r.get("label", ""), _fmt(r.get("bbox_norm")), r.get("position", ""),
                      _fmt(r.get("area_ha")), _pct(r.get("fraction")), _fmt(r.get("score"))]
                     for r in regions[:8]]
            out.append(Spacer(1, 4))
            out.append(_table(rows, [3.6 * cm, 4.4 * cm, 2.6 * cm, 2.0 * cm, 1.8 * cm, 1.6 * cm],
                              styles))

        labels = block.get("scene_labels")
        if labels:
            rows = [["adapted-head label", "probability", "above threshold"]]
            rows += [[l.get("label", ""), _fmt(l.get("probability")),
                      "yes" if l.get("above_threshold") else "no"] for l in labels[:8]]
            out.append(Spacer(1, 4))
            out.append(_table(rows, [8.0 * cm, 3.5 * cm, 4.5 * cm], styles))
            if block.get("label_source"):
                out.append(Paragraph(f"Label source: {block['label_source']}", styles["small"]))

        indices = block.get("indices")
        if indices:
            rows = [["index", "available", "mean", "p05", "p95", "threshold", "method", "separability"]]
            for i in indices[:12]:
                rows.append([i.get("name", ""), "yes" if i.get("available") else "no",
                             _fmt(i.get("mean")), _fmt(i.get("p05")), _fmt(i.get("p95")),
                             _fmt(i.get("threshold")), _fmt(i.get("threshold_method")),
                             _fmt(i.get("separability"))])
            out.append(Spacer(1, 4))
            out.append(_table(rows, [2.4 * cm, 1.6 * cm, 1.7 * cm, 1.6 * cm, 1.6 * cm, 1.9 * cm,
                                     3.6 * cm, 2.0 * cm], styles))

        quality = block.get("quality") or {}
        if quality.get("limitations"):
            out.append(Spacer(1, 4))
            out.append(Paragraph("<b>Stated limitations</b>", styles["body"]))
            for lim in quality["limitations"][:10]:
                out.append(Paragraph(f"• {lim}", styles["small"]))
        if quality.get("indices_unavailable"):
            out.append(Paragraph("Unavailable indices: " + ", ".join(quality["indices_unavailable"]),
                                 styles["small"]))
    return out


def _steps_section(trace: dict, styles) -> List[Any]:
    steps = trace.get("steps") or []
    if not steps:
        return []
    rows = [["#", "tool", "parameters", "outcome", "ms", "status"]]
    for s in steps:
        rows.append([s.get("order", ""), s.get("tool", ""),
                     _fmt({k: v for k, v in (s.get("parameters") or {}).items() if v is not None}),
                     s.get("outputs_summary", ""), s.get("duration_ms", ""),
                     s.get("status", "ok")])
    table = _table(rows, [0.9 * cm, 4.2 * cm, 4.6 * cm, 5.6 * cm, 1.2 * cm, 1.5 * cm], styles)
    for i, s in enumerate(steps, start=1):
        color = _STATUS_COLOR.get(s.get("status", "ok"))
        if color is not None:
            table.setStyle(TableStyle([("TEXTCOLOR", (5, i), (5, i), color)]))
    return [Paragraph("Executed tool sequence", styles["h2"]), table]


def _validation_section(trace: dict, styles) -> List[Any]:
    validation = trace.get("validation") or {}
    checks = validation.get("checks") or []
    if not checks:
        return []
    rows = [["check", "status", "message"]]
    for c in checks:
        rows.append([c.get("name", ""), c.get("status", ""), c.get("message", "")])
    table = _table(rows, [3.0 * cm, 1.9 * cm, 13.1 * cm], styles)
    for i, c in enumerate(checks, start=1):
        color = _STATUS_COLOR.get(c.get("status"))
        if color is not None:
            table.setStyle(TableStyle([("TEXTCOLOR", (1, i), (1, i), color)]))
    return [Paragraph("Input validation and compatibility checks", styles["h2"]), table]


def _routing_section(trace: dict, styles) -> List[Any]:
    cls = trace.get("classification") or {}
    entries = trace.get("registry_entries_used") or []
    rows = [["field", "value"],
            ["classified task", trace.get("task", "")],
            ["classifier method", cls.get("method", "")],
            ["plan", _fmt(trace.get("plan"))],
            ["registry entries used", _fmt([e.get("id") for e in entries])],
            ["model / tool", _fmt([e.get("model_id") for e in entries])],
            ["adapter", _fmt([e.get("adapter") for e in entries])],
            ["permitted parameters", _fmt(trace.get("effective_parameters"))],
            ["rejected parameters", _fmt([p for e in entries for p in (e.get("rejected_parameters") or [])])],
            ["runner-up task", _fmt((cls.get("runner_up") or {}).get("task"))],
            ["infeasible tasks", _fmt(list((cls.get("infeasible_tasks") or {}).keys()))],
            ["narration source", (trace.get("outputs") or {}).get("text", {}).get("narration_source", "")],
            ["backend", trace.get("model_backend", "")],
            ["execution time", f"{trace.get('execution_time_ms', '—')} ms"]]
    out = [Paragraph("Routing decision (the graded artefact)", styles["h2"]),
           _table(rows, [4.6 * cm, 13.4 * cm], styles)]
    reasons = cls.get("reasons") or []
    if reasons:
        out.append(Spacer(1, 3))
        for r in reasons[:6]:
            out.append(Paragraph(f"• {r}", styles["small"]))
    return out


def _evidence_section(result: dict, trace: dict, styles) -> List[Any]:
    items = (trace.get("outputs") or {}).get("evidence") or []
    if not items:
        items = [{"role": "input_preview", "path": p, "caption": Path(p).name}
                 for p in (result.get("evidence") or []) if isinstance(p, str)]
    out: List[Any] = [Paragraph("Visual evidence", styles["h2"])]
    shown = 0
    for item in items:
        path = Path(item.get("path", "")) if isinstance(item, dict) else Path(str(item))
        if not path.exists():
            continue
        try:
            from PIL import Image as PILImage

            with PILImage.open(path) as im:
                w, h = im.size
        except Exception:
            continue
        max_w = 15.5 * cm
        width = max_w
        height = width * h / max(w, 1)
        if height > 11 * cm:
            height = 11 * cm
            width = height * w / max(h, 1)
        caption = item.get("caption") if isinstance(item, dict) else ""
        role = item.get("role", "evidence") if isinstance(item, dict) else "evidence"
        out.append(KeepTogether([
            Image(str(path), width=width, height=height),
            Paragraph(f"<b>{role}</b> — {caption or path.name}", styles["small"]),
            Spacer(1, 6)]))
        shown += 1
        if shown >= 6:
            break
    if shown == 0:
        out.append(Paragraph("No rendered evidence for this run.", styles["small"]))
    return out


def build_report(run_id: str, result: dict, trace: dict, out_dir: Optional[Path] = None) -> Path:
    """Render the full report for one run."""
    out_dir = Path(out_dir or OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{run_id}.pdf"
    styles = _styles()
    doc = SimpleDocTemplate(str(dest), pagesize=A4, topMargin=1.3 * cm, bottomMargin=1.3 * cm,
                            leftMargin=1.4 * cm, rightMargin=1.4 * cm,
                            title=f"SatQuery AI report {run_id}", author="SatQuery AI")

    task = result.get("task") or trace.get("task") or "unknown"
    story: List[Any] = [
        Paragraph("SatQuery AI — Analysis Report", styles["title"]),
        Paragraph(f"Job {run_id} · task <b>{task}</b> · schema {trace.get('schema_version', 'n/a')} · "
                  f"generated {trace.get('created_at', '')} · "
                  f"SIH problem statement 26167 (ISRO/SAC)", styles["subtitle"]),
        Paragraph("Query", styles["h2"]),
        Paragraph(result.get("query") or trace.get("query") or "—", styles["body"]),
        Paragraph("Answer", styles["h2"]),
    ]

    text = result.get("text")
    if not text:
        rejected = result.get("rejected") or {}
        text = rejected.get("title") or "No answer was produced."
    story.append(Paragraph(text, styles["answer"]))

    rejected = result.get("rejected") or {}
    if rejected.get("validation_failed"):
        story.append(Spacer(1, 4))
        story.append(Paragraph("Rejection reasons", styles["h2"]))
        for reason in rejected["validation_failed"]:
            story.append(Paragraph(f"• {reason}", styles["body"]))

    story += _confidence_section(result.get("confidence") or trace.get("confidence") or {}, styles)
    story += _routing_section(trace, styles)
    story += _validation_section(trace, styles)
    story += _steps_section(trace, styles)
    story.append(PageBreak())
    story += _measurement_sections(result.get("measurements") or trace.get("measurements") or {},
                                   styles)
    story.append(PageBreak())
    story += _evidence_section(result, trace, styles)

    if trace:
        story.append(PageBreak())
        story.append(Paragraph("Audit trace (evaluated artefact, verbatim)", styles["h2"]))
        blob = json.dumps(trace, indent=1, default=str)
        for chunk in _chunks(blob, 3500):
            story.append(XPreformatted(_escape(chunk), styles["mono"]))
    doc.build(story)
    return dest


def _chunks(text: str, size: int):
    for i in range(0, len(text), size):
        yield text[i:i + size]


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
