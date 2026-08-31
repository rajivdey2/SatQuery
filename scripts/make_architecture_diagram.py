"""Render docs/architecture.png.

CLAUDE.md's repo layout calls for an architecture image next to the README. Drawing
it from code rather than pasting a screenshot means it cannot drift out of date
silently -- the box labels come from the actual registry, so a new task or a renamed
tool shows up in the diagram the next time this runs.

Usage:  python scripts/make_architecture_diagram.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from backend.controller.registry import REGISTRY  # noqa: E402

W, H = 1600, 1080
BG = (13, 17, 23)
PANEL = (24, 33, 48)
PANEL_2 = (19, 26, 35)
LINE = (37, 49, 67)
TEXT = (230, 237, 243)
MUTED = (139, 152, 169)
ACCENT = (74, 158, 255)
OK = (63, 185, 80)
WARN = (210, 153, 34)


def _font(size: int, bold: bool = False):
    names = (["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold
             else ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"])
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


F_TITLE = _font(30, True)
F_SUB = _font(15)
F_H = _font(17, True)
F_B = _font(13)
F_S = _font(11)


def box(dr: ImageDraw.ImageDraw, xy: Tuple[int, int, int, int], title: str,
        lines: Sequence[str] = (), fill=PANEL, border=LINE, title_color=TEXT,
        title_font=F_H) -> None:
    x1, y1, x2, y2 = xy
    dr.rounded_rectangle(xy, radius=9, fill=fill, outline=border, width=1)
    dr.text((x1 + 14, y1 + 10), title, font=title_font, fill=title_color)
    y = y1 + 12 + (title_font.size + 8)
    for line in lines:
        color = MUTED if line.startswith("·") or line.startswith("  ") else TEXT
        dr.text((x1 + 14, y), line, font=F_B if not line.startswith("  ") else F_S, fill=color)
        y += 18 if not line.startswith("  ") else 15


def arrow(dr: ImageDraw.ImageDraw, start: Tuple[int, int], end: Tuple[int, int],
          color=ACCENT, label: str = "") -> None:
    dr.line([start, end], fill=color, width=2)
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = max((dx * dx + dy * dy) ** 0.5, 1e-6)
    ux, uy = dx / length, dy / length
    tip = end
    left = (end[0] - 9 * ux - 5 * uy, end[1] - 9 * uy + 5 * ux)
    right = (end[0] - 9 * ux + 5 * uy, end[1] - 9 * uy - 5 * ux)
    dr.polygon([tip, left, right], fill=color)
    if label:
        mx, my = (start[0] + end[0]) // 2, (start[1] + end[1]) // 2
        dr.text((mx + 8, my - 16), label, font=F_S, fill=MUTED)


def main() -> None:
    img = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(img)

    dr.text((40, 30), "SatQuery AI — agentic remote-sensing vision-language assistant",
            font=F_TITLE, fill=TEXT)
    dr.text((42, 70), "SIH problem statement 26167 (ISRO/SAC) · the graded artefact is the "
                      "observable execution trace", font=F_SUB, fill=MUTED)

    # ---- input ----
    box(dr, (40, 120, 330, 250), "Input", [
        "1–2 images + a query",
        "· GeoTIFF / TIFF (geospatial)",
        "· PNG / JPEG (benchmarks only)",
        "· single · bi-temporal pair",
        "· co-registered optical+SAR pair",
    ], fill=PANEL_2)

    # ---- controller ----
    box(dr, (380, 120, 900, 560), "Agentic controller", [
        "1  Input validator      format · modality · CRS",
        "     co-registration · acquisition dates",
        "     → rejects and explains, never assumes",
        "2  Task classifier      scored rules over",
        "     wording × input configuration",
        "     → task + runner-up + infeasible tasks",
        "3  Router               predefined registry:",
        "     required inputs + typed parameter schema",
        "     → unpermitted parameters named, not dropped",
        "4  Executor             plan of 1–2 entries,",
        "     every tool step timed and recorded",
        "5  Combiner + confidence  measured signals only,",
        "     otherwise 'not available'",
        "6  Audit trace          schema 2.0.0 JSON",
    ])
    arrow(dr, (330, 185), (380, 185))

    # ---- specialists ----
    tasks = {}
    for entry in REGISTRY:
        tasks.setdefault(entry.specialist, []).append(entry.task)
    specialist_titles = {
        "rs_vlm": "RS-VLM specialist",
        "change": "Change specialist",
        "fusion": "Optical–SAR fusion",
    }
    x = 950
    for key in ("rs_vlm", "change", "fusion"):
        lines = [f"· {t}" for t in tasks.get(key, [])]
        extra = {
            "rs_vlm": ["", "boxes in 0–100 space", "class map overlay"],
            "change": ["", "CVA + noise floor", "per-class Δ in hectares", "change map"],
            "fusion": ["", "Cohen κ per class", "obscured-area recovery", "joint class map"],
        }[key]
        box(dr, (x, 120, x + 200, 380), specialist_titles[key], lines + extra, fill=PANEL_2)
        arrow(dr, (900, 200 + 60 * (("rs_vlm", "change", "fusion").index(key))), (x, 180))
        x += 215

    # ---- analysis engine ----
    box(dr, (950, 410, 1560, 640), "Measurement engine (analysis/)", [
        "indices      NDVI · NDWI · MNDWI · NDBI · visible proxies · SAR dB + roughness",
        "thresholds   physical prior + Otsu, with the separability recorded",
        "landcover    water · vegetation · built-up · bare soil, each with its evidence basis",
        "regions      connected components → boxes, positions, hectares",
        "change       radiometric normalisation → change vectors → transitions",
        "fusion       independent per-sensor measurement → agreement → joint map",
        "scene_labels adapted BigEarthNet-MM dual-branch head (19 classes)",
        "render       class map · grounding overlay · change triptych · fusion map",
    ], fill=PANEL)
    arrow(dr, (1050, 380), (1050, 410))

    box(dr, (950, 670, 1560, 790), "Narration", [
        "may only restate measured values",
        "· templates (no GPU needed)",
        "· or Qwen3-VL + optional LoRA adapter, constrained to the measurements",
        "turning the VLM off costs fluency, not correctness",
    ], fill=PANEL_2)
    arrow(dr, (1250, 640), (1250, 670))

    # ---- outputs ----
    box(dr, (380, 600, 900, 900), "Outputs", [
        "answer text (every number traceable to a threshold)",
        "bounding boxes · change map · joint class map",
        "confidence + the individual signals behind it",
        "audit trace JSON  ← the graded artefact",
        "downloadable PDF report (answer, checks, tools,",
        "  measurements, evidence, trace verbatim)",
        "",
        "GUI: React SPA · FastAPI backend",
    ], fill=PANEL_2)
    arrow(dr, (640, 560), (640, 600))

    # ---- adaptation ----
    box(dr, (40, 300, 330, 560), "Remote-sensing adaptation", [
        "training/adapt_ben_mm.py",
        "  BigEarthNet-MM dual-branch head",
        "  19-class CORINE nomenclature",
        "  NumPy · CPU · held-out metrics",
        "",
        "training/run_lora_train.py",
        "  4-bit QLoRA on Qwen3-VL",
        "  RSVQAxBEN → VRSBench → RSVG",
        "  CDVQA + LEVIR-CC · BigEarthNet-MM",
        "",
        "training/eval/run_benchmarks.py",
        "  RSVQA · VRSBench · CDVQA",
    ], fill=PANEL_2, title_color=OK)

    box(dr, (40, 600, 330, 900), "Honesty rules", [
        "· unverifiable co-registration → reject",
        "· no NIR → NDVI reported unavailable,",
        "    visible proxy used, reliability down",
        "· single-pol SAR → vegetation not claimed",
        "· uncalibrated DN → scene-relative",
        "    thresholds, stated in the trace",
        "· no measurable signal → confidence",
        "    'not available', never invented",
        "· confidence is never marked calibrated",
    ], fill=PANEL_2, title_color=WARN)

    dr.text((40, 940), "Everything the problem statement grades — task, tools from the predefined "
                       "registry, permitted parameters, outputs, confidence — is emitted as JSON,",
            font=F_B, fill=MUTED)
    dr.text((40, 962), "rendered in the GUI without opening developer tools, and embedded verbatim "
                       "in the downloadable report.", font=F_B, fill=MUTED)

    out = Path(__file__).resolve().parent.parent / "docs" / "architecture.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"architecture diagram -> {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
