# SatQuery AI

**An agentic vision-language assistant for multimodal remote-sensing image analysis
through natural-language queries.**

SIH problem statement **26167** — ISRO / Space Applications Centre, Department of
Space. *Software · Space Technology.*

![architecture](docs/architecture.png)

SatQuery AI is not a single fine-tuned VLM. It is an **agentic controller** that
reads a plain-language query, validates the imagery it was given, selects one or
more specialists from a **predefined registry**, measures the scene, and returns an
evidence-grounded answer together with the **auditable execution trace** that the
problem statement actually grades.

It runs on a laptop. No GPU, no downloaded weights, no internet.

---

## Why it is built this way

Three sentences in the problem statement drove every architectural decision.

**1. "Only the observable execution trace … will be evaluated. Internal reasoning
text is neither required nor evaluated."**
So the JSON trace is a first-class deliverable, not a debug log. It records the
routed task, the registry entries used, the permitted parameters, every input check,
the executed tool sequence with timings, the measurements, and the confidence with
its individual signals. It is rendered in the GUI without opening developer tools,
downloadable, and embedded verbatim in the PDF report.

**2. Final evaluation uses "an ISRO/SAC evaluation dataset … Cartosat-2S optical and
RISAT SAR image pairs" that participants never see.**
So input validation and honest uncertainty matter as much as accuracy. The system
rejects pairs it cannot verify as co-registered, reports which spectral indices a
product cannot supply, and declines to claim classes a sensor cannot discriminate.

**3. "A generic LLM or VLM without remote-sensing adaptation will not satisfy the
requirements."**
So the numbers are produced by a measurement engine, and adaptation is real: a
dual-branch Sentinel-1 + Sentinel-2 land-cover head trained on BigEarthNet-MM, plus
optional Qwen3-VL LoRA adapters for wording.

### Measure, then speak

Every specialist does the same two things in the same order:

1. **Measure.** Raw bands → spectral/radar indices → thresholds with a recorded
   separability → class masks → connected regions → extents in hectares. Typed
   records, straight into the trace.
2. **Speak.** The narration layer may only restate measured values. Templates do it
   with no GPU; Qwen3-VL does it more fluently when available, constrained to the
   same numbers.

Turning the VLM off costs fluency, not correctness — and every number in an answer
can be traced back to a threshold and a pixel count.

---

## Mandatory scope → where it lives

| Requirement (problem statement) | Implementation | State |
|---|---|---|
| Remote-sensing adaptation using BigEarthNet | `training/adapt_ben_mm.py` — dual-branch S1+S2 multi-label head, 19-class BigEarthNet nomenclature, held-out metrics | code complete; run it to produce `models/ben_mm_lc.npz` |
| Single-image **VQA** (mandatory) | `specialists/rs_vlm.py` → `single_vqa` | working |
| Second single-image task — **grounding** | `single_grounding` → boxes in 0–100 coordinates + overlay | working |
| Second single-image task — captioning | `single_caption` (both implemented, not just one) | working |
| **Change** description / change-VQA (mandatory) | `specialists/change.py` → `change_description`, `change_vqa` | working |
| Spatial change map (optional extra) | before/after/change triptych with clusters | working |
| **Optical–SAR** cross-modal extraction (mandatory) | `specialists/fusion.py` → per-class Cohen's κ, obscured-area recovery | working |
| **Agentic orchestration** from a predefined registry | `controller/registry.py`, `controller/executor.py` — typed parameter schemas, multi-entry plans | working |
| Input upload + compatibility checking | `controller/validator.py` — format, modality, CRS, co-registration, dates | working |
| Confidence information | `controller/confidence.py` — measured signals only, `not_available` otherwise | working |
| Execution summary / audit trace | `controller/audit.py` — schema 2.0.0 | working |
| Interactive GUI | React + Vite SPA | working |
| Downloadable reports | `reports/pdf_report.py` | working |
| GeoTIFF/TIFF; PNG/JPEG for benchmarks only | enforced and flagged in the validator | working |

---

## Quickstart

```bash
python -m venv venv && venv\Scripts\activate       # Windows
pip install -r backend/requirements.txt

python scripts/make_demo_data.py      # synthetic scenes with known ground truth
python scripts/build_examples.py      # bake the showcase examples
uvicorn backend.api.main:app --port 8000
```

```bash
cd frontend && npm install && npm run dev          # http://localhost:5173
```

The dashboard opens with **eight pre-computed examples** — the five representative
queries from the problem statement plus three cases that show input rejection,
two-entry orchestration, and graceful degradation on a benchmark PNG. Click one to
see the stored result instantly, or **run live** to recompute it.

### Verify it

```bash
python scripts/verify_all.py           # imports, compile, routing, examples, API, tests
```

One command, `PASS`/`FAIL` per stage, non-zero exit on failure. `--demo-ready` for
the fast subset. Individually:

```bash
python scripts/smoke_controller.py     # all five queries + edge cases, with routing
python scripts/test_api.py             # HTTP surface, in-process, no server needed
python -m pytest tests -q              # measurements, controller, API, adapted head
```

### Optional backends

```powershell
# Qwen3-VL narration (needs torch + transformers + a GPU)
$env:USE_REAL_MODEL = "1"; $env:MODEL_ID = "Qwen/Qwen3-VL-4B-Instruct"
$env:ADAPTER_RS_VLM = "models/rs_vlm_adapter"     # after a QLoRA run

# UI-only walkthrough: template phrasing over real measurements, labelled as mock
$env:SATQUERY_MOCK = "1"
```

All knobs are in `backend/config.py`.

---

## What the measurement engine actually measures

| Module | Produces |
|---|---|
| `preprocessing/bands.py` | band roles from product descriptions → sensor convention → statistical tie-break for the ambiguous 4-band (Cartosat-2S MX) case |
| `preprocessing/sar_ops.py` | radiometric convention detection (dB / amplitude / intensity), refined-Lee speckle filter, ENL estimate, absolute-calibration plausibility |
| `analysis/indices.py` | NDVI, NDWI, MNDWI, NDBI, visible-only proxies, SAR dB + roughness + polarimetric ratio — and *why* each unavailable index is unavailable |
| `analysis/thresholds.py` | Otsu with its between-class variance ratio; physical priors that the data may only override inside a plausible window; dark-mode split for minority classes |
| `analysis/landcover.py` | water / vegetation / built-up / bare soil, each with its evidence basis and a reliability grade |
| `analysis/regions.py` | connected components → boxes, compass positions, hectares |
| `analysis/change.py` | relative radiometric normalisation, change vector analysis with a noise floor, per-class deltas with a significance test, transition matrix |
| `analysis/fusion.py` | independent per-sensor measurement, per-class IoU and Cohen's κ, cloud/haze recovery from radar |
| `analysis/grounding.py` | referring expression → class + qualifiers → selected regions |
| `analysis/scene_labels.py` | adapted BigEarthNet-MM head inference (shares its feature extractor with the trainer) |

### Honesty rules, enforced in code

- Footprints overlapping < 50%, or a GSD ratio > 8× → **rejected**, with the measured overlap in the trace.
- Same acquisition date on a bi-temporal pair → **rejected**.
- No NIR band → NDVI/NDWI reported **unavailable**, visible proxies used, reliability downgraded, confidence penalised.
- Single-polarisation SAR → vegetation **not claimed**.
- Uncalibrated SAR digital numbers → scene-relative thresholds, stated as such.
- No measurable signal → confidence `not_available`, never a fabricated number.
- `confidence.calibrated` is **always false**, with a note explaining that it is a documented composite of measured quantities, not a fitted probability.

---

## Evaluation

`training/eval/run_benchmarks.py` runs two backends over the same protocol: the
measurement engine (`--backend analysis`, no GPU) and Qwen3-VL with an optional
adapter (`--backend vlm`). Results are written to
`training/eval/results/<dataset>_<backend>_<timestamp>.json`.

| Benchmark | Task | Metric | Measurement engine | + LoRA adapter | Run |
|---|---|---|---|---|---|
| RSVQA (LR/HR) | VQA | accuracy, per question type | not yet run | not yet run | — |
| VRSBench | VQA | accuracy | not yet run | not yet run | — |
| VRSBench | grounding | Acc@0.5 / Acc@0.7 | not yet run | not yet run | — |
| VRSBench | captioning | BLEU-4 / METEOR / CIDEr / ROUGE-L (`pycocoevalcap`) | not yet run | not yet run | — |
| CDVQA | change-VQA | per-type acc, AA, OA (test1 + test2) | not yet run | not yet run | — |
| BigEarthNet-MM | adapted head | micro-F1 / macro-F1 / mAP, held out | not yet run | n/a | — |

These rows are deliberately empty rather than estimated. The benchmark archives are
not bundled; §C of `docs/TRAINING.md` has the exact commands, and every result file
carries a timestamp to cite.

Meanwhile the **synthetic demo scenes have known ground truth**
(`backend/runtime/demo_data/ground_truth.json`), so `tests/test_analysis.py` asserts
the engine recovers the true class extents, the true built-up expansion and the true
water loss — self-consistency you can check without downloading anything.

---

## Repository layout

```
satquery/
├── CLAUDE.md                    spec, architecture, dataset plan (read first)
├── README.md
├── docs/
│   ├── architecture.md          request path, data model, gates, failure behaviour
│   ├── architecture.png         generated by scripts/make_architecture_diagram.py
│   ├── TRAINING.md              how to train the adapted head and the LoRA adapters
│   ├── DEMO_RUNBOOK.md          setup, fallbacks and a 7-minute presentation script
│   └── PPT_CONTEXT.md           citation-linked context for slides
├── backend/
│   ├── controller/              validator · classifier · registry · executor
│   │                            combiner · confidence · audit
│   ├── analysis/                the measurement engine (see the table above)
│   ├── specialists/             rs_vlm · change · fusion, over one interface
│   │   └── backends/            model_loader (Qwen3-VL + logit margin) · vlm · mock
│   ├── preprocessing/           bands · geotiff_io · sar_ops · pipeline
│   ├── api/                     FastAPI routes, async jobs, examples
│   ├── reports/                 PDF generation
│   └── config.py                every knob, environment-driven
├── frontend/                    React + Vite SPA
├── training/
│   ├── adapt_ben_mm.py          BigEarthNet-MM adapted head (CPU, mandatory)
│   ├── run_lora_train.py        QLoRA on Qwen3-VL (GPU, optional)
│   ├── data_prep/               one converter per dataset in CLAUDE.md §3
│   ├── lora_configs/            rank 64 / alpha 128, staged
│   └── eval/                    benchmark harness + local judge
├── scripts/
│   ├── make_demo_data.py        synthetic scenes with ground truth
│   ├── build_examples.py        bake the dashboard's showcase examples
│   ├── verify_all.py            one-command end-to-end verification
│   ├── smoke_controller.py      five queries + edge cases, with the routing table
│   ├── test_api.py              HTTP smoke, in-process
│   └── make_architecture_diagram.py
└── tests/                       pytest: preprocessing, analysis, controller, API, head
```

---

## Datasets

Training and adaptation: **BigEarthNet-MM / reBEN** (co-registered Sentinel-1 +
Sentinel-2, CORINE multi-labels), **RSVQAxBEN** (VQA built directly on BigEarthNet
patches). Single-image: **VRSBench**, **RSVQA**, **RSVG/RSVGD**. Change:
**CDVQA**, **LEVIR-CC / LEVIR-CD**. One converter per dataset in
`training/data_prep/`; sources and commands in `docs/TRAINING.md` §B.2.

## References

GeoChat (CVPR 2024, arXiv:2311.15826) · VRSBench (arXiv:2406.12384) · RSVQA (Lobry
et al., IEEE TGRS 2020) · CDVQA (arXiv:2112.06343) · reBEN / BigEarthNet v2.0
(arXiv:2407.03653) · self-supervised SAR–optical fusion (Chen & Bruzzone,
arXiv:2103.05543) · ChangeChat (arXiv:2409.08582) · EarthGPT (arXiv:2401.16822).
Threshold priors: McFeeters 1996 (NDWI), Xu 2006 (MNDWI), Zha 2003 (NDBI), Otsu 1979.
