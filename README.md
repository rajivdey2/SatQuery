# SatQuery AI

**An interactive vision-language assistant for multimodal remote-sensing image analysis through natural-language queries.**

SIH Problem Statement **26167** (ISRO/SAC, Department of Space) — *Software · Space Technology*.

SatQuery AI is **not** a single fine-tuned VLM. It is an **agentic controller** that routes natural-language queries to the right remote-sensing specialist — over three input regimes — validates inputs, executes a predefined model/tool workflow, combines textual and spatial evidence, estimates confidence, and emits an **auditable JSON execution trace**. Only the observable execution trace is graded; internal reasoning is not evaluated. This system is therefore designed so the trace is a first-class deliverable, visible in the UI, and downloadable as a PDF report.

```
  User query + images         AGENTIC CONTROLLER                                    OUTPUT
 ─────────────────────────▶  1. Input Validator       ── modalities, CRS/extent,   evidence-grounded
                             2. Task Classifier       ── dates, format checks        text + boxes/masks
                             3. Tool/Model Router     ── predefined registry ──▶   + confidence
                             4. Executor              ── permitted params only    + AUDIT TRACE (JSON)
                             5. Combiner + Confidence ── real signal or "N/A"
                             6. Audit Trace Logger    ── visible in GUI + PDF
```

## Why this design (driven by the actual grading)

- The problem statement grades the **observable execution trace**: task classified, models/tools selected *from a predefined registry*, parameters used, outputs, confidence. A slick chain-of-thought prompt is worth zero points.
- Final evaluation happens on **ISRO's own unseen Cartosat-2S (optical) + RISAT (SAR) pairs**. Compatibility checking and honest uncertainty reporting matter as much as model accuracy — a system that says "I can't do this reliably" on an unfamiliar product scores better than one that silently hallucinates.
- Specialists are **swappable behind one `predict()` interface** (CLAUDE.md §2). The orchestration works against mocks from day one; real adapted models drop in without touching the controller.

## Mandatory scope → feature map

| Mandatory requirement (PS) | Where it lives | Status |
|---|---|---|
| RS adaptation (fine-tuned/adapted on BigEarthNet) | `training/` — **rs_vlm LoRA**: RSVQAxBEN (VQA built directly on BigEarthNet patches) → VRSBench slice (QLoRA, rank 64) | In build |
| Single-image **VQA** (mandatory) | `specialists/rs_vlm.py` → task `single_vqa` | Zero-shot today, LoRA adapter ships Day 2 |
| Second single-image task: **text-guided grounding** | task `single_grounding` (native box output → SVG overlay) | Zero-shot today |
| Change analysis: **change-VQA / change description** (bi-temporal pair) | `specialists/change.py` → `change_vqa` / `change_description` (two-image prompt) | Zero-shot today, flagged in trace |
| **Optical–SAR cross-modal analysis** | `specialists/fusion.py` → `sar_optical_fusion` (tagged two-image prompt) | Zero-shot today, flagged in trace |
| **Agentic orchestration** (select/sequence from predefined registry) | `controller/registry.py` + `executor.py` | ✔ complete |
| Input/compatibility checking (format, modality, co-registration, dates) | `controller/validator.py` | ✔ complete |
| Evidence + confidence + **auditable execution summary** | `controller/audit.py`, `combiner.py` | ✔ complete |
| Interactive GUI + **downloadable reports** | `api/` + `frontend/` + `reports/pdf_report.py` | ✔ complete |
| Supported formats: GeoTIFF/TIFF; PNG/JPEG only for benchmark data | `controller/validator.py` | ✔ complete |

## Repository layout

```
satquery-ai/
├── CLAUDE.md                     # spec, architecture, dataset plan (read first)
├── README.md                     # this file
├── docs/
│   ├── PPT_CONTEXT.md            # citation-linked context for your judges' PPT
│   └── architecture.md           # ASCII architecture diagram
├── backend/
│   ├── api/                      # FastAPI routes (async jobs, report, static)
│   ├── controller/               # validator · classifier · registry · executor · combiner · audit
│   ├── specialists/              # base predict() interface · rs_vlm · change · fusion · zero_shot · mock
│   ├── preprocessing/            # geotiff_io (rasterio) · sar_ops (speckle/dB/pseudo-RGB)
│   ├── reports/                  # reportlab PDF generation with audit trace
│   ├── config.py                 # env-driven settings (model id, adapters, mock flag)
│   └── requirements.txt          # runtime deps (no torch by default)
├── frontend/                     # React + Vite SPA (upload, chat, evidence, audit, report)
└── training/
    ├── data_prep/                # RSVQAxBEN subset builder, VRSBench slice, CDVQA notes
    ├── lora_configs/             # QLoRA config for Qwen3-VL-4B specialists
    ├── run_lora_train.py         # TRL SFTTrainer entrypoint (Kaggle/Colab)
    └── eval/                     # eval harness: VRSBench/RSVQA/CDVQA + local judge
```

## Quickstart (mock backend — no GPU needed)

```bash
# backend
cd backend
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt          # fastapi, uvicorn, rasterio, numpy, pillow, reportlab
uvicorn api.main:app --reload --port 8000

# frontend
cd frontend
npm install
npm run dev        # http://localhost:5173
```

Default is **mock specialists** (deterministic, GPU-free, demo-safe). To use the real Qwen3-VL 4-bit zero-shot backend:

```powershell
$env:USE_REAL_MODEL = "1"
$env:MODEL_ID = "Qwen/Qwen3-VL-4B-Instruct"     # or a path to your local adapter
uvicorn api.main:app --port 8000
```

See `backend/config.py` for all knobs.

## Evaluation (fills weekly; update this table when you rerun `training/eval/`)

`training/eval/` runs official test splits (or documented slices when the full split is too heavy for a free GPU) and reports the protocols exactly as the official benchmarks define them — **including the GPT-based semantic judge** that VRSBench and SARLANG use (here: local open judge model, replayable offline). Timestamp every run; the README table is the credibility artifact judges ask about.

| Benchmark | Task | Metric | Zero-shot | RS-VLM LoRA (adapter) | Runs |
|---|---|---|---|---|---|
| **VRSBench** | VQA | Accuracy (exact + local-judge semantic) | — | — | — |
| **VRSBench** | Grounding | Acc@0.5 / Acc@0.7 (all/uniquе) | — | — | — |
| **RSVQA (LR/HR)** | VQA | Accuracy | — | — | — |
| **CDVQA** | Change-VQA | Per-type acc, AA, OA (test1 + test2) | — | — | — |
| **SARLANG-1M** | SAR VQA (fusion/SAR sanity) | GPT-judge accuracy | — | — | — |

> Grounding coordinates on VRSBench are normalized to **0–100**. CDVQA has **two official test splits** (test1/test2) — report both.

### Build status — 29 Aug 2026 (mock backend, graded core shipped)

The full graded core is implemented and verified end-to-end: validator, 6-task classifier, predefined registry, executor, combiner/confidence policy, audit-trace JSON (schema v1.0.0), FastAPI, PDF reports, and the React SPA. All five verbatim demo queries ran through the live API:

| # | Query (verbatim PS) | Routed task | Evidence | Confidence | API round-trip |
|---|---|---|---|---|---|
| 1 | "Describe the land-cover and major objects visible..." | `single_caption` | thumbnail | mock (placeholder) | ✅ + PDF |
| 2 | "Highlight the water body referred to..." | `single_grounding` | 1 `<box>` overlay | mock (placeholder) | ✅ + PDF |
| 3 | "What changed between these two dates...?" | `change_vqa` | dates 2023-01-01→07-01, co-reg 100% | mock (placeholder) | ✅ + PDF |
| 4 | "Use the optical and SAR images together..." | `sar_optical_fusion` | optical+SAR pair, co-reg 100% (EPSG:32644) | mock (placeholder) | ✅ + PDF |
| 5 | "Has the built-up area increased, decreased...?" | `change_vqa` | bi-temporal pair | mock (placeholder) | ✅ + PDF |

Verifier cases, also green: unsupported/multi-modal input rejection, mismatched-CRS pair **rejection with explanation** (validator gate), unknown single-band SAR flagged. Timings: 25–130 ms per job (mock; PNG/TIFF both exercised).
Reproduce with `python scripts/smoke_controller.py` and `python scripts/test_api.py` (server up).

### Roadmap to real (needs GPU time)
- `rs_vlm` QLoRA: `training/data_prep/rsvqaxben_to_sft.py` → `vrsbench_to_sft.py` → `training/run_lora_train.py` (rank 64/α128, 1 epoch) → drop adapter in `ADAPTER_RS_VLM` → audit trace switches `mock`→`adapter`.
- `change` (CDVQA+LEVIR-CC) and `fusion` (BigEarthNet-MM) use the same trainer; until then the audit honestly reports them as zero-shot / mock.

## Demo script (judges; the PS representative queries, verbatim)

1. *"Describe the land-cover and major objects visible in this image."* → `single_caption`
2. *"Highlight the water body referred to in the query."* → `single_grounding` — show the box overlay
3. *"What changed between these two dates, and where did the change occur?"* → `change_description`
4. *"Use the optical and SAR images together to identify built-up and water-covered regions."* → `sar_optical_fusion`
5. *"Has the built-up area increased, decreased, or remained unchanged?"* → `change_vqa`

For each: show the answer + visual evidence, then **open the audit trace panel** — the routing decision is the graded artifact, don't rush it. Then download the PDF report.

## 2-day build plan (this sprint)

- **Day 1 — graded core:** controller (6 stages), GeoTIFF/SAR preprocessing, FastAPI + PDF, React SPA, mock+zero-shot run of the five queries → screenshots/audit samples for PPT.
- **Day 2 — real adaptation:** single QLoRA run of **rs_vlm** on RSVQAxBEN → VRSBench (1 GPU account); drop-in wiring; eval snapshot; README table refresh; demo rehearsal. `change`/`fusion` adapters remain zero-shot and **are explicitly flagged as such in the audit trace** (honesty > fabricated confidence, per the PS).

## Risks (see `docs/PPT_CONTEXT.md` for full register)

| Risk | Mitigation |
|---|---|
| ISRO Cartosat/RISAT preprocessing differs from benchmarks | Validator checks modality/metadata defensively; SAR path applies dB + speckle + per-scene normalize; sanity-check on outside optical–SAR pairs if reachable before finale |
| 2-day timebox | Graded core ships Day 1 against mocks; only one adapter trained; everything else honestly flagged |
| VRSBench GPT-judge metric | Local open judge model, reproducible offline; exact-match reported alongside |
| PS dataset link inconsistent (`arxiv.org/abs/2603.29630`) | Use canonical BigEarthNet v2.0/reBEN (`bigearth.net`, arXiv:2407.03653); documented in PPT_CONTEXT |

## References (full list with links in `docs/PPT_CONTEXT.md`)

Qwen3-VL · Qwen3.5 · reBEN (BigEarthNet v2.0) · RSVQAxBEN · VRSBench · RSVQA · CDVQA · LEVIR-CC/CD/MCI · QAG-360K · SARLANG-1M · SARChat · SARVLM · Earth-OneVision · EarthMind · RSThinker/Geo-CoT · RSCoVLM · Change-Agent · GeoChat