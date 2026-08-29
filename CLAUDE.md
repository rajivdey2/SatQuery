# SatQuery AI — Build Plan (SIH Problem Statement 26167, ISRO/SAC)

> Read this whole file before writing code. It is the spec, the architecture, the dataset
> plan, and the win condition for this project. Treat every "MANDATORY" tag as a hard
> requirement — the judging rubric fails the whole submission if any is missing.

## 0. What this problem actually is

SatQuery AI is **not** "fine-tune a VLM on satellite images." It is an **agentic
controller that routes natural-language queries to the right remote-sensing specialist
model(s)**, over three input regimes:

1. **Single image** (optical/multispectral OR SAR) — VQA (mandatory) + one of
   {captioning, grounding}.
2. **Bi-temporal pair** (same place, two dates) — change description or change-VQA
   (mandatory), optional change map.
3. **Cross-modal pair** (co-registered optical + SAR, same place/time) — joint
   extraction (e.g. built-up + water together).

The eval set is **not just public benchmarks** — ISRO/SAC will test on their own
**Cartosat-2S optical + RISAT SAR** pairs, unseen, unlabeled to us. That single fact
should drive most of the architecture decisions below: generalization and honest
uncertainty reporting beat benchmark-chasing.

## 1. Read this before writing a single model call — what actually gets scored

> "The controller may perform internal task planning; however, only the observable
> execution trace... will be evaluated. Internal reasoning text is neither required nor
> evaluated."

This is the sentence most competing teams will skim past. It means:

- A slick chain-of-thought prompt is worth **zero** points on its own.
- The **JSON execution trace** — task classified, models/tools selected, parameters
  used, outputs, confidence — is a first-class deliverable, not a debug log. Design its
  schema in week 1, not as an afterthought.
- Since grading partly happens on ISRO's own imagery, **the compatibility checker and
  confidence estimator matter as much as the specialist models.** A system that
  correctly says "I can't do this reliably" on unfamiliar SAR product formats scores
  better than one that silently hallucinates.

**Design the audit-trail schema and the demo around this insight.** It's the cheapest,
highest-leverage differentiator available — most teams will over-invest in model
accuracy and under-invest in the orchestration trace and input validation that ISRO
explicitly said they'd grade.

## 2. System architecture

```
                        ┌─────────────────────────────┐
   User query + image(s) │        Agentic Controller     │
   ─────────────────────▶│  1. Input Validator            │
                        │  2. Task Classifier            │
                        │  3. Tool/Model Router           │
                        │  4. Executor                    │
                        │  5. Output Combiner + Confidence│
                        │  6. Audit Trace Logger          │
                        └───────┬─────────┬─────────┬─────┘
                                │         │         │
                 ┌──────────────┘   ┌─────┘    ┌─────┘
                 ▼                  ▼          ▼
        ┌────────────────┐ ┌────────────────┐ ┌──────────────────────┐
        │ RS-VLM          │ │ Change          │ │ Optical–SAR Fusion   │
        │ Specialist      │ │ Specialist      │ │ Specialist            │
        │ (VQA, Caption,  │ │ (Change-VQA,    │ │ (dual-encoder,        │
        │  Grounding)     │ │  Change desc.,  │ │  joint extraction)    │
        │                 │ │  change map)    │ │                       │
        └────────────────┘ └────────────────┘ └──────────────────────┘
                 │                  │                    │
                 └────────► Visual evidence + text ◀─────┘
                              (boxes, masks, prose)
```

Everything below the Controller is a **swappable specialist behind a common interface**
— `predict(images, modality_tags, query, task) -> {text, boxes?, mask?, confidence,
model_id, params}`. Build the interface and a stub/mock specialist FIRST, wire the
Controller and UI against the mock, then replace mocks with real models one at a time.
This decouples "does the orchestration work" from "is the model accurate" — the two
things judges actually score separately.

## 3. Datasets — what to pull, and why each one

MANDATORY (per problem statement): fine-tune/adapt on **BigEarthNet**, evaluate on
**VRSBench + RSVQA** (single-image) and **CDVQA** (change).

| Dataset | Use | Notes |
|---|---|---|
| **BigEarthNet-MM (aka BigEarthNet v1/v2, reBEN)** | Domain adaptation — co-registered Sentinel-1 (SAR) + Sentinel-2 (optical) patches, CORINE land-cover multi-labels | 590k pairs (v1) / 549k pairs (reBEN, cleaner). This is your **cross-modal fusion pretraining source** — it is literally SAR+optical pairs of the same place. Get it from `bigearth.net` or Zenodo (reBEN). |
| **RSVQAxBEN** | VQA pairs generated directly on BigEarthNet patches | Bridges BigEarthNet → VQA task format for free — use this to bootstrap your VQA specialist's domain adaptation before touching VRSBench. |
| **VRSBench** | Eval + SFT — captioning, grounding, VQA | 29,614 images, human-verified captions, 52,472 referring expressions, 123,221 QA pairs. This is your **primary single-image SFT set** for the mandatory VQA + your chosen second task (grounding recommended — see §4). `vrsbench.github.io` |
| **RSVQA (LR/HR)** | Eval | Lobry et al. benchmark — official eval target named in the PS. |
| **CDVQA** | Eval + SFT — change-VQA | Built on SECOND dataset, 2,968 bi-temporal pairs, ~122k auto-generated QA pairs, 6 land-cover change classes. Official eval target named in the PS. |
| **LEVIR-CC / LEVIR-CD / LEVIR-MCI** | SFT — change captioning + change masks | 10,077 bi-temporal pairs with free-text change descriptions (LEVIR-CC) and pixel masks (LEVIR-CD/MCI). Use this to teach the model *why* something changed, not just yes/no. |
| **QAG-360K** | Optional SFT boost | 360k+ {question, answer, pixel mask} triplets across LEVIR-CD/SECOND/Hi-UCD — richer change QA than CDVQA alone if you have time budget. |
| **RSVG / RSVGD (DIOR-based)** | SFT — grounding | Referring-expression → bounding box pairs for the region-grounding task. |
| **Sen1-2 / SEN12MS** | Optional robustness set | Extra optical–SAR pairs *not* from BigEarthNet, used purely to sanity-check the fusion specialist doesn't only work on BigEarthNet's exact preprocessing — cheap insurance against the "unseen ISRO data" risk in §1. |

**Do not skip the RSVQAxBEN → BigEarthNet bootstrapping step.** It's the dataset that
literally satisfies "fine-tuned using BigEarthNet.txt" in the mandatory scope while
also being VQA-shaped, so one training run covers two requirements.

## 4. Component 1 — RS-VLM specialist (VQA baseline + captioning/grounding)

**Backbone recommendation: Qwen2-VL-7B-Instruct (or Qwen2.5-VL if available in your
environment) over LLaVA-1.5.** Reasoning: GeoChat (the reference architecture most
judges will have seen — CVPR 2024, LLaVA-1.5 + LoRA, box-in-text grounding) proved the
recipe works, but it hand-rolled a text-based bounding-box format. Qwen2-VL/2.5-VL
ships **native grounding support** (point/box referring baked into pretraining), which
cuts your grounding-head engineering time roughly in half — meaningful when you're
also building two other specialists. If Qwen-VL isn't available/quota-limited in your
compute environment, fall back to LLaVA-1.5-7B + the GeoChat recipe (well-documented,
reproducible).

- **Mandatory task:** VQA — free-form Q&A grounded in a single image.
- **Second task (pick one):** grounding is recommended over captioning — it produces
  visual evidence (a box on the image), which directly satisfies the PS requirement for
  "visual evidence" in the final output and demos far better than a caption does.
- **Fine-tuning method:** LoRA (rank 64, alpha 128 — matches published RS-VLM configs
  like ChangeChat), not full fine-tune. Freeze vision tower + projector, LoRA on the LLM
  attention layers only. This runs on a single 24–48GB GPU (or 4-bit QLoRA on a free-tier
  16GB Colab/Kaggle GPU) in hours, not days — critical given hackathon compute limits.
- **Training data order:** RSVQAxBEN (domain bootstrap) → VRSBench VQA+captions →
  VRSBench referring expressions + RSVG (grounding). One epoch each is enough per the
  published recipes (RS-GPT4V, ChangeChat both converge in ~1 epoch at this LoRA rank).
- **Eval:** report VRSBench (A@0.5/A@0.7 for grounding, accuracy for VQA) and RSVQA
  official splits. Put this table in your README — judges explicitly named these as the
  eval benchmarks, so showing you already benchmarked yourself is free credibility.

## 5. Component 2 — Change specialist (change-VQA / change description, mandatory)

- **Architecture shortcut:** don't build a bespoke Siamese network from scratch. Feed
  the pre-image and post-image as **two images in one multi-image prompt** to the same
  backbone from §4 ("Image 1 is the earlier acquisition, Image 2 is the later
  acquisition. <question>"). Recent work (native multimodal Qwen models on Change-VQA)
  shows this interleaved-image approach is competitive with custom Siamese
  cross-attention architectures, at a fraction of the engineering cost.
- **Fine-tune on:** CDVQA (mandatory eval target — SECOND-based, 6 land-cover change
  classes) + LEVIR-CC (free-text change captions, teaches *what* and *where* changed,
  not just *whether*) + QAG-360K if time allows (adds pixel-mask supervision).
- **Optional stretch (explicitly allowed by the PS: "a spatial change map may also be
  generated where reference masks are available"):** a small separate lightweight
  Siamese CNN (e.g. a compact U-Net or a BIT-style transformer, trained on LEVIR-CD
  masks) that outputs a literal change mask overlay. This is a strong demo moment —
  "highlight where the built-up area grew" with an actual heatmap — but it's optional;
  don't build it until VQA + change-VQA are solid.
- **This specialist directly answers two of the PS's representative queries:**
  *"What changed between these two dates, and where did the change occur?"* and
  *"Has the built-up area increased, decreased, or remained unchanged?"*
  — test against those exact phrasings early.

## 6. Component 3 — Optical–SAR fusion specialist (cross-modal pair, mandatory)

This is the hardest component and the one most teams will do worst on — lean into it
as your differentiator.

- **Preprocessing (do this properly, it matters more than model choice):**
  - SAR: apply speckle filtering (Lee or refined-Lee filter), convert amplitude to dB
    scale, normalize per-scene (SAR dynamic range varies wildly by acquisition).
  - Optical: standard atmospheric-corrected reflectance normalization (BigEarthNet
    patches are already Level-2A corrected).
  - **Co-registration check is part of your input validator, not an assumption** — the
    PS explicitly separates "co-registered pairs" as the defined input; verify CRS +
    extent overlap before running fusion, and reject/flag pairs that fail this check.
    This is exactly the kind of observable-trace behavior §1 rewards.
- **Fusion strategy (two-stage, ranked by effort vs. payoff):**
  1. **Baseline (build first):** dual-branch feature extractor — CLIP-ish ViT for
     optical, a separate lightweight CNN branch for SAR (SAR has fundamentally
     different statistics; a shared encoder underperforms) — concatenate features,
     feed into the same LLM backbone as a two-image multi-modal-tagged prompt
     ("Image 1 is optical.", "Image 2 is SAR."). Fine-tune on BigEarthNet-MM pairs,
     converted to VQA-style prompts using the CORINE labels as weak supervision
     ("What land-cover classes are present using both images?").
  2. **Stretch:** self-supervised contrastive pretraining of the SAR branch against the
     optical branch on unlabeled BigEarthNet-MM pairs first (multi-view contrastive
     loss, image-level + patch-level, following the published SAR-optical
     self-supervised fusion recipe), *then* the supervised fine-tune above. This is
     what separates "concatenated two images" from "the SAR branch actually learned
     complementary structure" — worth doing if the baseline works and you have time
     left, skip it otherwise.
- **This specialist directly answers:** *"Use the optical and SAR images together to
  identify built-up and water-covered regions."*

## 7. Component 4 — Agentic Controller (the actual novelty, don't shortcut this)

This is what the PS is actually judging as "the solution," per §1. Build it as its own
clean module, not glue code.

**Pipeline, each stage independently testable and independently logged:**

1. **Input Validator** — count images, detect modality (optical vs SAR: check band
   count/dtype/metadata tags, don't guess from pixel stats alone if metadata is
   present), verify format (GeoTIFF/TIFF via `rasterio`/GDAL; PNG/JPEG only for
   benchmark inputs, per PS spec), check co-registration for pairs (CRS + extent),
   check temporal metadata for bi-temporal pairs (acquisition dates differ). **Reject
   and explain, don't silently proceed, on any failure** — this behavior alone will
   stand out against teams that assume clean input.
2. **Task Classifier** — map the natural-language query + validated input
   configuration to one of: `single_vqa`, `single_caption`, `single_grounding`,
   `change_vqa`, `change_description`, `sar_optical_fusion`. Start with a small
   few-shot-prompted LLM classifier (cheap, fast to iterate); if you have eval time
   left, replace with a fine-tuned lightweight text classifier for speed/determinism at
   the finale demo (deterministic beats clever when you're live-demoing to judges).
3. **Router** — a predefined **model/tool registry** (explicit dict: task → specialist
   + required inputs + permitted parameters), not free-form tool invention by an LLM.
   The PS says "select one or more models or tools **from a predefined registry**" —
   take that literally, it constrains what you should build.
4. **Executor** — runs the selected specialist(s) with only the permitted parameters,
   sequenced if a query needs more than one (e.g. fusion output feeding into a VQA
   restatement).
5. **Combiner + Confidence** — merge textual output with spatial output (boxes/masks),
   compute a confidence score (simplest defensible version: softmax/logit margin from
   the specialist, or agreement score if you ran an ensemble check) — don't fabricate a
   number, and say so if you can't estimate one honestly for a given task.
6. **Audit Trace** — emit a structured JSON: `{task, models_used[], parameters,
   validation_checks_passed, confidence, execution_time_ms}`. Render this **visibly in
   the UI**, not just in logs — it's evaluated, so it must be visible to evaluators
   without opening devtools.

## 8. Frontend / GUI (MANDATORY: "interactive GUI or web application")

- **Stack:** FastAPI backend (async, background tasks for slow GeoTIFF inference) +
  React/Next.js frontend, or Streamlit if the team is optimizing for build speed over
  polish (Streamlit is faster to get working, React demos better to judges — pick
  based on remaining time, don't do both).
- **Must show:** image upload (GeoTIFF-aware preview via `rasterio` → PNG thumbnail),
  chat/query box, rendered visual evidence (bounding boxes / change-map overlay drawn
  on the image, not just described in text), the **audit trace panel** from §7.6, a
  confidence indicator, and a "download report" button (a simple generated PDF with
  query, answer, evidence images, and the audit trace — the PS explicitly lists
  "downloadable reports" as expected).
- **Map context is a nice-to-have, not mandatory** — if there's time, overlay
  georeferenced results on a Leaflet/deck.gl map using the GeoTIFF's CRS; skip this
  before skipping anything in §7.

## 9. Suggested repo structure

```
satquery-ai/
├── CLAUDE.md                  # this file
├── README.md                  # eval tables, demo gif, architecture diagram
├── backend/
│   ├── controller/
│   │   ├── validator.py       # §7.1
│   │   ├── classifier.py      # §7.2
│   │   ├── registry.py        # §7.3 — the predefined model/tool registry
│   │   ├── executor.py        # §7.4
│   │   └── audit.py           # §7.5–7.6, JSON schema lives here
│   ├── specialists/
│   │   ├── base.py            # common predict() interface — build first, mock it
│   │   ├── rs_vlm.py           # §4
│   │   ├── change.py           # §5
│   │   └── fusion.py           # §6
│   ├── preprocessing/
│   │   ├── geotiff_io.py      # rasterio/GDAL helpers
│   │   └── sar_ops.py         # speckle filter, dB scaling
│   ├── api/                   # FastAPI routes
│   └── reports/               # PDF report generation
├── training/
│   ├── data_prep/              # dataset → SFT-format converters, one per dataset in §3
│   ├── lora_configs/            # per-specialist LoRA yaml configs
│   └── eval/                   # benchmark harness, §12
├── frontend/
└── docs/architecture.png
```

## 10. Build order (front-load the thing that's actually graded)

1. **Week 0:** Controller interface + mock specialists + audit-trace schema + minimal
   UI wired end-to-end. This is deliberately first — it de-risks the part in §1 that's
   explicitly graded, and gives you a demoable (if dumb) product from day one.
2. **Week 1:** RS-VLM specialist — VQA on RSVQAxBEN → VRSBench. Get this real and wired
   into the controller, replacing its mock.
3. **Week 2:** Grounding fine-tune on top of the same backbone; wire visual-evidence
   rendering in the UI.
4. **Week 2–3 (parallel if team size allows):** Change specialist on CDVQA + LEVIR-CC.
5. **Week 3–4:** Fusion specialist on BigEarthNet-MM (baseline dual-branch first,
   contrastive stretch only if ahead of schedule).
6. **Week 4:** Input validator hardening (real GeoTIFF edge cases, co-registration
   checks, SAR-format sniffing) — this is where ISRO's own Cartosat/RISAT data will
   punish shortcuts, so don't leave it for the finale.
7. **Finale (36h, if selected):** don't build new specialists. Budget the time as:
   report/PDF export polish, demo script rehearsal (§13), eval table refresh, and
   robustness testing against any sample ISRO-format imagery released at the venue.

## 11. Compute strategy for limited GPU time

- 4-bit QLoRA on a single free/Colab-tier 16GB GPU is sufficient for all three
  specialists at 7B scale, one epoch per dataset stage, per the published configs cited
  in §4–§6 (rank 64, alpha 128, AdamW, cosine schedule, ~2e-4 LR, mixed precision).
- Train the RS-VLM specialist's VQA capability first and validate the whole pipeline
  end-to-end on it before spending GPU hours on the other two specialists — a working
  end-to-end demo with one strong specialist beats three half-trained ones.
- Cache preprocessed BigEarthNet/VRSBench tensors to disk after first pass; re-running
  data loading from raw GeoTIFF every epoch will eat your compute budget fast.

## 12. Evaluation & benchmark harness (build this, don't skip it)

Write one script per official benchmark (VRSBench, RSVQA, CDVQA) that loads the
official test split and reports the standard metric (accuracy for VQA/change-VQA,
BLEU-4/METEOR/CIDEr/ROUGE-L for captioning, A@0.5/A@0.7 IoU accuracy for grounding).
Put the resulting table in the README with a timestamp. This does three things: proves
the "fine-tuned on BigEarthNet" mandatory requirement actually improved something,
gives judges a number instead of a vibe, and catches regressions before the finale.

## 13. Demo script for judges (write this in week 1, refine throughout)

Walk through the five representative queries from the PS verbatim, in order, live:
1. *"Describe the land-cover and major objects visible in this image."* (VQA/caption)
2. *"Highlight the water body referred to in the query."* (grounding — show the box)
3. *"What changed between these two dates, and where did the change occur?"* (change)
4. *"Use the optical and SAR images together to identify built-up and water-covered
   regions."* (fusion)
5. *"Has the built-up area increased, decreased, or remained unchanged?"* (change-VQA)

For each: show the query, the rendered answer + visual evidence, **and open the audit
trace panel** so judges see the routing decision explicitly — this is the moment that
maps directly onto §1's grading insight, make sure it's not rushed.

## 14. Risk register

| Risk | Mitigation |
|---|---|
| ISRO's real Cartosat/RISAT imagery differs from BigEarthNet/benchmark preprocessing | Sanity-test the fusion + change specialists on an *outside* optical-SAR pair source (Sen1-2/SEN12MS) before the finale, not just BigEarthNet-derived val sets |
| GPU/compute runs out mid-build | Build order in §10 is ordered so a partial build still demos end-to-end; never leave the controller/UI for last |
| GeoTIFF/CRS handling breaks on edge-case files | Budget explicit time for this in §10 week 4 — it's boring, unglamorous, and exactly where judges will find your bug if you don't |
| Team over-invests in one flashy specialist, mandatory scope incomplete | Re-read §0's numbered list weekly; every one of the three input regimes must work, even minimally, before polishing any single one further |
| Confidence score is fabricated/meaningless | Only report confidence where you have a real signal (logit margin, ensemble agreement); say "not available" otherwise — an honest gap beats a fake number if judges probe it |

## 15. References

- GeoChat (CVPR 2024) — grounded RS VLM, LLaVA-1.5 + LoRA reference architecture:
  arxiv.org/abs/2311.15826
- EarthGPT / EarthGPT-X — unified optical+SAR+infrared MLLM, MMRS-1M dataset:
  arxiv.org/abs/2401.16822, arxiv.org/abs/2504.12795
- VRSBench — captioning/grounding/VQA benchmark: arxiv.org/abs/2406.12384,
  vrsbench.github.io
- RSVQA — Lobry et al., IEEE TGRS 2020 (VQA benchmark named in the PS)
- CDVQA — Yuan et al., arxiv.org/abs/2112.06343 (change-VQA benchmark named in the PS)
- QAG-360K, LEVIR-CC/CD/MCI — change captioning + pixel-mask change QA datasets
- BigEarthNet-MM / reBEN — co-registered Sentinel-1/2 archive:
  bigearth.net, arxiv.org/abs/2407.03653 (reBEN)
- RSVQAxBEN — VQA pairs generated directly on BigEarthNet patches
- Self-supervised SAR-optical fusion (Chen & Bruzzone) — contrastive fusion recipe:
  arxiv.org/abs/2103.05543
- ChangeChat — LoRA config reference (rank 64, alpha 128) for RS instruction tuning:
  arxiv.org/abs/2409.08582
