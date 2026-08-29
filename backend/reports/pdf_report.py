"""PDF report generator (reportlab).

"Download report" is an explicit expectation of the problem statement: query,
answer, evidence image with overlays, the graded audit trace, and confidence.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer

from backend.config import OUTPUT_DIR

_BOX = colors.HexColor("#d50000")


def _section(title: str, body: str) -> list:
    styles = getSampleStyleSheet()
    h = ParagraphStyle("h", parent=styles["Heading3"], spaceBefore=8, spaceAfter=4)
    p = ParagraphStyle("p", parent=styles["BodyText"], fontSize=9, leading=13)
    return [Paragraph(title, h), Paragraph(body, p)]


def build_report(run_id: str, result: dict, trace: dict, out_dir: Optional[Path] = None) -> Path:
    """Create run_id.pdf from an API result dict + audit trace dict."""
    out_dir = out_dir or OUTPUT_DIR
    dest = out_dir / f"{run_id}.pdf"
    doc = SimpleDocTemplate(str(dest), pagesize=A4,
                            topMargin=1.5 * cm, bottomMargin=1.5 * cm,
                            leftMargin=1.5 * cm, rightMargin=1.5 * cm)
    styles = getSampleStyleSheet()
    story = [Paragraph("SatQuery AI - Analysis Report", styles["Title"]),
             Paragraph(f"Job {run_id} &nbsp;|&nbsp; task: {result.get('task', trace.get('task', ''))}",
                       styles["Italic"]),
             Spacer(1, 6)]

    story += _section("Query", result.get("query", ""))

    text = result.get("text")
    if text is None or (isinstance(text, list) and not text):
        text = result.get("rejected", {}).get("title", "No answer (input rejected).")
    story += _section("Answer", text if isinstance(text, str) else " ".join(str(t) for t in text))

    conf = result.get("confidence") or {}
    story += _section("Confidence", f"{conf.get('source', 'not_available')} — {conf.get('note', '')}")

    evidence = result.get("evidence") or []
    import tempfile
    for idx, path in enumerate(evidence):
        p = Path(path)
        if not p.exists():
            continue
        display = p
        boxes = result.get("boxes") or []
        if boxes:
            display = _overlay(path, boxes)
        img = Image(str(p), width=14 * cm, height=9.5 * cm)
        if display != p:
            img = Image(str(display), width=14 * cm, height=9.5 * cm)
        story.append(img)
        story.append(Paragraph(f"<i>Evidence {idx + 1}: {Path(path).name}</i>", styles["Italic"]))
    if result.get("mask_path"):
        story.append(Paragraph(f"Change map: {result['mask_path']}", styles["Italic"]))

    if trace:
        story.append(Paragraph("Audit trace (evaluated artifact)", styles["Heading2"]))
        story.append(Paragraph(
            "<pre>" + json.dumps(trace, indent=2)[:12000] + "</pre>",
            ParagraphStyle("pre", fontName="Courier", fontSize=6.5, leading=8.5)))
    doc.build(story)
    return dest


def _overlay(image_path: str, boxes: list, out_dir: Optional[Path] = None) -> Path:
    """Draw normalized 0..100 boxes onto a copy of the evidence image."""
    from PIL import Image as PILImage, ImageDraw

    out_dir = out_dir or OUTPUT_DIR
    src = Path(image_path)
    img = PILImage.open(src).convert("RGB")
    w, h = img.size
    dr = ImageDraw.Draw(img)
    for b in boxes:
        x1, y1, x2, y2 = b.get("bbox", [0, 0, 0, 0])
        if max(x1, y1, x2, y2) > 100:
            continue
        px1, py1 = x1 / 100.0 * w, y1 / 100.0 * h
        px2, py2 = x2 / 100.0 * w, y2 / 100.0 * h
        dr.rectangle([px1, py1, px2, py2], outline=(213, 0, 0), width=max(2, w // 500))
        label = b.get("label") or ""
        if label:
            dr.text((px1 + 2, max(0, py1 - 12)), label[:40], fill=(213, 0, 0))
    dest = out_dir / f"{src.stem}_overlay.png"
    img.save(dest)
    return dest