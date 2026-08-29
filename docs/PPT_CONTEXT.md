# SatQuery AI — PPT / Judge Context (source of talking points)

> Use this file as the backing notes for your submission PPT and for slide text.
> Every claim is citation-linked so you can defend any slide under Q&A.
> Maintain it as the build progresses — "Status" fields are updated live.

---

## 0. The problem in one paragraph (PS ID 26167, ISRO/SAC)

> "SatQuery AI — An Interactive Vision-Language Assistant for Multimodal Remote Sensing Image Analysis through Text Queries."

Remote-sensing AI today is fragmented: one tool per task (classification, detection, VQA, change detection), and non-experts must learn sensor physics, GIS workflows, and model parameters to extract anything from an image. A generic LLM/VLM cannot do specialist RS work reliably without **domain adaptation**. SatQuery AI is an **agentic, query-driven framework**: it routes a natural-language query over one of three input regimes to the correct remote-sensing specialist model(s), validates inputs, sequences tools, combines textual + spatial outputs, and returns an evidence-grounded answer with an **auditable execution summary**.

**Input regimes (defined in the PS):**
1. **Single image** — optical/multispectral or SAR: captioning, VQA, text-guided region grounding (VQA **mandatory**, plus one of the other two).
2. **Bi-temporal pair** (same place, two dates): change description / change-VQA (**mandatory**); a spatial change map is optional where reference masks exist.
3. **Cross-modal pair** (co-registered optical + SAR, same place/time): joint extraction + cross-modal analysis (**mandatory**).
4. **Formats:** GeoTIFF/TIFF for geospatial imagery; PNG/JPEG only for the prescribed public benchmarks.

**The five representative queries (PS, verbatim)** — these are your demo skeleton:
1. "Describe the land-cover and major objects visible in this image."
2. "Highlight the water body referred to in the query."
3. "What changed between these two dates, and where did the change occur?"
4. "Use the optical and SAR images together to identify built-up and water-covered regions."
5. "Has the built-up area increased, decreased, or remained unchanged?"

---

## 1. THE grading insight every slide should orbit around

> "The controller may perform internal task planning; however, only the **observable execution trace**, including the selected task, models or tools, permitted parameters, and outputs will be evaluated. Internal reasoning text is neither required nor evaluated."

**Consequence (your lead slide):** the graded artifact is a **structured JSON execution trace** — task classified, registry entries selected, permitted parameters, outputs, confidence — rendered **visibly in the GUI** and downloadable as a report. A sophisticated chain-of-thought prompt is worth zero points. This is a cheap, high-leverage differentiator: most teams over-invest in model accuracy and under-invest in orchestration trace + input validation, which is what ISRO actually said they'd grade.

Second consequence: final evaluation uses **unseen ISRO data** — "pre-georeferenced and co-registered Cartosat-2S optical and RISAT SAR image pairs, with task-specific reference answers, labels, bounding boxes, or masks." Evaluation annotations are **not disclosed to teams**. So generalization + honest uncertainty beat benchmark-chasing. A system that says *"I can't do this reliably — co-registration check failed / SAR product format unexpected"* scores better than one that silently hallucinates. **The compatibility checker and confidence estimator are as important as the specialist models.**
→ Build the demo around the audit trace, not around model accuracy claims.

---

## 2. Tech decisions (2026 landscape — why we build this way)

### 2.1 Unify everything on ONE backbone + LoRA specialists (instead of three hand-built networks)

| Considered architecture | Verdict | Evidence / argument |
|---|---|---|
| GeoChat-style per-task VLM (LLaVA-1.5, box-in-text grounding) | Rejected as base | 2024 reference recipe, but hand-rolled box text format; superseded |
| **One frozen Qwen backbone + task LoRA adapters** (rs_vlm / change / fusion) | **Chosen** | Native grounding (boxes+points), native interleaved multi-image prompts, 256K context; "specialists" become swappable adapters behind one `predict()` interface — matches the PS "predefined registry" requirement cleanly |
| Bespoke Siamese CNN / dual-encoder contrastive fusion (CLAUDE.md §5–6 old plan) | Rejected for 2-day build | 2026 papers show multi-image prompts + LoRA beat handcrafted change architectures; a single 2B RSMLLM now unifies optical+SAR+fusion |

**Key citation — Change-VQA (2026):** "Revisiting Change VQA in Remote Sensing with Structured Vision–Language Design" — LoRA fine-tuning of **Qwen3.5-2B** (native multimodal, two temporal images in one prompt) beats the previous bespoke SOTA **VisTA**: CDVQA test1 AA **68.59 (vs 65.9)**, OA **74.74 (vs 73.1)**; test2 OA **70.94 (vs 68.5)**. The paper also finds **model size does not scale monotonically** with change-VQA performance, and native multimodal models beat structured vision–language pipelines for this task. → Our exact recipe (multi-image prompt + LoRA at small scale) is empirically, recently SOTA.

**Key citation — multi-modal unify (2026):** **Earth-OneVision** (2B RSMLLM) unifies optical, SAR, IR, multispectral, temporal, video and **cross-sensor fusion** in one autoregressive model with 34M QA pairs (MMRS-OneVision): 87.52% P@0.5 on OPT-RSVG grounding, 80.68% on SARLANG-Bench SAR VQA (beating 7B models by >7%), 75.74% recall on BigEarthNet-MS. Recipes we reuse:
- **SAR → pseudo-RGB** (replicate channels into RGB) — no special SAR encoder needed;
- **Multispectral bands regrouped into pseudo-RGB triplets** (12-band Sentinel-2 → RGB groups);
- **Progressive Cross-Modality Adaptation**: natural → optical RS → non-optical (viewpoint gap first, imaging-physics gap second).

### 2.2 Backbone choice: Qwen3-VL-4B-Instruct

- **Qwen3-VL** (Oct 2025): 2B/4B/8B/32B dense + MoE; native 2D grounding (boxes **and** points), interleaved multi-image, 256K context; DeepStack multi-level ViT features; strong agent/tool-use ability. Open weights, mature HF transformers/vLLM/SGLang support, LoRA recipes exist.
- **Qwen3.5** (Feb 2026): native multimodal (early text–vision fusion), hybrid Gated-DeltaNet/attention architecture, 0.8B–397B. Best CDVQA LoRA paper numbers — but newer, kernel-sensitive (`causal_conv1d`/`fla`), less training precedent. **Chosen as the change-specialist A/B candidate** if time/compute permits; Qwen3-VL-4B is the safe primary (mature, well-precedented RS recipes: RSCoVLM ships a Qwen3-VL branch).
- Constraint reality: **single free 16 GB GPU (Kaggle/Colab), 2 days** ⇒ everything is **4-bit QLoRA**, rank 64 / alpha 128 (ChangeChat/RS recipes), frozen vision tower, one epoch per stage.

### 2.3 The three specialists (each a LoRA adapter behind the same interface)

```
specialist.predict(images, modality_tags, query, task) -> {text, boxes?, mask?, confidence, model_id, params}
```

- **rs_vlm** — single-image: VQA (mandatory) + caption + **grounding** (native boxes → SVG overlay in UI). Data: RSVQAxBEN (VQA on BigEarthNet patches — doubles as the mandatory BigEarthNet adaptation) → VRSBench.
- **change** — bi-temporal: change-VQA + change description, **two images in one prompt** with temporal tags ("Image 1: earlier acquisition, Image 2: later"). Data: CDVQA (mandatory eval target) + LEVIR-CC/LEVIR-MCI (free-text change captions + change masks).
- **fusion** — optical+SAR: co-registered pair, modality-tagged two-image prompt ("Image 1 is optical, Image 2 is SAR"). Data: BigEarthNet-MM (S1+S2 pairs) with CORINE multi-labels turned into weak VQA text; SARLANG-1M / SARChat-2M slices to close the SAR-visual domain gap.
- **Optional tool** in registry (PS allows): lightweight Siamese change-map U-Net/BIT trained on LEVIR-CD/MCI masks → literal "where did it change" heatmap. **Stretch only.**

### 2.4 The agentic controller (= the graded novelty)

Exactly CLAUDE.md §7, six independent, independently-logged stages:
1. **Input Validator** — format gate (GeoTIFF/TIFF; PNG/JPEG only for benchmarks); **modality sniff from bands/dtype/metadata tags, not pixel stats**; co-registration check for pairs (CRS + extent overlap + pixel size); temporal-metadata check for bi-temporal (dates differ). **Reject-and-explain, never silently proceed.**
2. **Task Classifier** — few-shot LLM with a **deterministic JSON-schema fallback**; constrained output to the six PS tasks (`single_vqa, single_caption, single_grounding, change_vqa, change_description, sar_optical_fusion`). Determinism beats clever at a live demo.
3. **Router** — **predefined registry** (explicit dict: task → specialist + required inputs + permitted params). The PS says "select one or more models or tools **from a predefined registry**" — we take this literally.
4. **Executor** — runs specialist(s) with permitted parameters only, sequencing if needed.
5. **Combiner + Confidence** — merge text with boxes/masks; confidence only from a **real signal** (answer-token margin / self-consistency / validator gating); otherwise honest **"not available"**.
6. **Audit Trace** — structured JSON `{task, model_id, adapter, params, validation_checks, confidence, execution_time_ms, outputs}` rendered in the GUI + downloadable PDF.

---

## 3. Datasets — table with links (slide-ready)

| Dataset | What it is | Size / format | Use in our plan | Link |
|---|---|---|---|---|
| **BigEarthNet v2.0 / reBEN** | Co-registered **S1 (SAR, dB) + S2 (optical, 12-band)** patches, CORINE-2018 labels, pixel reference maps, geographically-decorrelated split | 549,488 pairs, 1200 m @10 m; `rico-hdl` DL format | Mandatory RS adaptation; fusion pair source | bigearth.net · arXiv:2407.03653 · Zenodo 10891137 |
| **RSVQAxBEN** | VQA pairs generated directly on BigEarthNet patches (presence/LC/logical questions) | 590,326 S2 patches, ~15M QA (25 Q/patch) | **One run satisfies "fine-tuned on BigEarthNet" + VQA**; bootstrap rs_vlm | rsvqa.sylvainlobry.com · GitHub syvlo/RSVQAxBEN · Zenodo 5083737/8 |
| **VRSBench** | Caption + referring + VQA benchmark (human-verified; DOTA/DIOR splits) | 29,614 img; train 20,264 / test 9,350; 52,472 refers; 123,221 VQA | Primary single-image SFT + eval (VQA mandatory eval) | vrsbench.github.io · HF xiang709/VRSBench · arXiv:2406.12384 |
| **RSVQA (LR/HR)** | Official RSVQA benchmark (OSM-derived Q/A) | LR: 9 S2 tiles NL; HR: 10.6k USGS 15 cm patches | Named eval target (single-image VQA) | rsvqa.sylvainlobry.com · IEEE TGRS 2020 |
| **CDVQA** | Change-VQA on SECOND public subset, **2 test splits (test1/test2)** | 2,968 pairs (512×512), 122k QA, 6 land-cover change classes | Mandatory change eval + change SFT | arXiv:2112.06343 |
| **LEVIR-CC / CD / MCI** | Free-text change captions; binary change masks; both | 10,077 pairs (256×256 @0.5 m), 5 captions/pair + masks | Change SFT (teaches *what/where* changed); change-map tool source | LEVIR lab page · HF lcybuaa/LEVIR-MCI · GitHub Chen-Yang-Liu/Change-Agent |
| **QAG-360K** | {question, answer, pixel mask} over LEVIR-CD/SECOND/Hi-UCD | 360k+ | Optional change-QA boost | — |
| **RSVG / DIOR-RSVG** | Referring-expression grounding | 4,239 / 17,402 img | Grounding SFT (if time) | DIOR-RSVG arXiv:2306.05488 |
| **SARLANG-1M** | SAR caption + VQA benchmark (concise/detailed, 7 applications, 16 LC classes) | 118,331 img, 1.1M+ human-verified texts (0.1–25 m) | Close the SAR visual gap; SAR VQA sanity eval | arXiv:2504.03254 · GitHub Jimmyxichen/SARLANG-1M · HF YiminJimmy/SARLANG-1M |
| **SARChat-2M** | SAR instruction dialogues (classification/description/counting/localizing/referring) | ~2M pairs (0.3–10 m) | Optional SAR SFT | arXiv:2502.08168 · GitHub JimmyMa99/SARChat |
| **SARVLM-1M** | SAR caption dataset (two-stage optical→SAR transfer recipe) | 1.7M pairs | Optional SAR SFT | arXiv:2510.22665 |
| **VRSBench-SAR** | GPT-5.1 auto-annotations over SARDet-100k | — | Optional SAR SFT/eval | HF xiang709/VRSBench-SAR |
| **Sen1-2 / SEN12MS** | Extra optical–SAR pairs (non-BigEarthNet preprocessing) | — | Robustness sanity-check — "unseen data" insurance | — |

> **⚠️ Link-verification note for Q&A:** the PS dataset link is `arxiv.org/abs/2603.29630` for "BigEarthNet.txt". The **canonical** references are **BigEarthNet v1 (arXiv:1902.10903)** and the refined **BigEarthNet v2.0/reBEN (arXiv:2407.03653)** at **bigearth.net** (+ Zenodo 10891137). Cite the canonical ones; mention that the PS link appears to be a placeholder.

---

## 4. Model landscape (2026) — what exists and why we didn't just take one

| Model | Who/When | What it proves for us | Link |
|---|---|---|---|
| **Qwen3-VL** | Qwen, Oct 2025 | Native grounding + multi-image + 256K context; our base | arXiv:2511.21631 · github QwenLM/Qwen3-VL |
| **Qwen3.5** | Qwen, Feb 2026 | Native multimodal; best CDVQA-LoRA paper numbers; change-specialist candidate | HF Qwen/Qwen3.5-2B · blog alibabacloud qwen3.5 |
| **CDVQA + LoRA study** | Apr 2026 | LoRA on Qwen3.5-2B beats VisTA on both CDVQA test splits | arXiv:2604.18429 |
| **Earth-OneVision** | 2026 | 2B unifies 6 sensor modalities + cross-sensor fusion; recipes: SAR pseudo-RGB, band-grouped triplets, progressive modality adaptation | arXiv:2606.10819 |
| **EarthMind** | 2025 | Optical-SAR LMM w/ cross-modal fusion + ground-truth-answered multi-granular benchmark (EarthMind-Bench) | arXiv:2506.01667 |
| **EarthGPT-X** | Apr 2025 / TGRS | Optical+SAR+IR spatial MLLM, visual-prompting (points/boxes/scribbles), referring+grounding unified; M-RSVP data | arXiv:2504.12795 · IEEE TGRS 2025 |
| **SARVLM** | 2026 | SAR VL foundation model via two-stage optical→SAR transfer + parameter ensemble | arXiv:2510.22665 |
| **SARLANG-1M / SARChat-2M** | 2025/26 | SAR caption/VQA instruction data + benchmarks; pre- vs post-fine-tune gaps | arXiv:2504.03254 · arXiv:2502.08168 |
| **RSThinker / Geo-CoT** | ICLR 2026 | Verifiable reasoning *traces* via SFT + GRPO — validates our "trace is the artifact" philosophy for RS | openreview lJ7zecny2e |
| **RSCoVLM** | 2025 | Qwen2.5-VL/Qwen3-VL based RS-VLM, open recipe + eval tooling (reference for our harness) | github VisionXLab/RSCoVLM |
| **Change-Agent / MCI** | 2024 | Eyes+brain change interpretation; LEVIR-MCI data (change masks + captions); conceptual ancestor of our change specialist + change-map tool | arXiv:2403.19646 · IEEE TGRS 2024 |

**One-sentence defense:** "We adapt a modern open VLM (Qwen3-VL) with task LoRA adapters, because in 2026 the published evidence — Qwen3.5-2B beating VisTA on CDVQA, and Earth-OneVision at 2B unifying optical+SAR+fusion — shows the multi-image-prompt + domain-tuned small-model approach is both competitive and achievable on hackathon compute, and it lets us invest engineering in the agentic orchestration + audit trace that the problem actually grades."

---

## 5. Evaluation protocol gotchas (put these in the bench slides)

- **VRSBench captioning & VQA use a GPT-based semantic judge** (CLAIR for captions; GPT-4 semantic-match for VQA to accept synonyms). We replicate with a **local open judge model** — offline, reproducible, no API key on the contest floor; exact-match reported alongside.
- **VRSBench grounding coordinates are normalized to 0–100** (2026 update note on the repo) — adapters must output/consume that convention or map it.
- **CDVQA has two official test sub-splits** (test1: 39,686 QA / test2: 31,036 QA) with per-question-type accuracy + Average Accuracy + Overall Accuracy. Report both splits (many papers only report one).
- Metrics per task: VQA/change-VQA → accuracy; caption → BLEU-4/METEOR/ROUGE-L/CIDEr (+CLAIR); grounding → Acc@0.5/0.7 (unique/non-unique/all); fusion/SAR → BigEarthNet classification recall or SARLANG GPT-accuracy.
- Run the official eval scripts (VRSBench repo, RSVQA repo, CDVQA protocol) and timestamp every run. Judges "normalise scores before combining metrics" — don't cherry-pick a subset.

---

## 6. Confidence & honesty policy (anti-hallucination slide)

- Confidence is only reported when backed by a real signal: **answer-token logit margin**, **self-consistency across 2 passes**, or **validator gating** (e.g., co-registration passed).
- Otherwise the field is the literal string **"not available"** — and the audit trace explains why.
- Zero-shot specialists are **explicitly tagged** (`adapter: none, fallback: zero-shot`) in the trace; adapted ones carry `adapter: <name>, checkpoint: <id>`.
- Slide line: *"When we can't estimate reliability honestly, we say so — the problem statement rewards that more than a fabricated number."* (Directly matches the PS's ISRO-data caveat.)

---

## 7. Risk register (PPT slide condensed)

| Risk | Mitigation |
|---|---|
| ISRO Cartosat-2S/RISAT preprocessing ≠ benchmark preprocessing | Validator infers modality from metadata/bands; SAR path does dB + Lee speckle + per-scene normalize; optional Sen1-2/SEN12MS robustness check |
| 2-day timebox | Graded core (controller + validator + audit + GUI + PDF) ships first on mocks; one LoRA adapter (rs_vlm) real; change/fusion honestly flagged zero-shot |
| VRSBench GPT-judge metric | Local judge, offline-reproducible; exact-match baseline also reported |
| GeoTIFF/CRS edge cases | Explicit validator hardening pass; reject-and-explain paths tested with synthetic + real GeoTIFFs |
| Compute exhaustion | Order of builds ensures partial artifacts still demo end-to-end; QLoRA 4-bit single epoch per stage |
| Fabricated confidence | §6 policy enforced in `combiner.py` + audit schema |

---

## 7b. Build status (2026-08-29) — graded core is DONE and verified

- Backend controller (validate → classify → registry → execute → combine/confidence → audit) fully implemented, mock+zero-shot runnable, schema v1.0.0.
- Live API round-trip verified for all five verbatim PS queries; PDF reports download; mismatched-CRS pair rejected at the validator gate with explanation.
- Demo artifacts exist for screenshots: `backend/runtime/{traces,outputs,demo_data}` + `scripts/{smoke_controller,test_api,make_demo_data}.py`.
- Remaining GPU work (single Colab/Kaggle session, one rs_vlm QLoRA run): the 3 converters → `training/run_lora_train.py` → wire via `ADAPTER_RS_VLM`, refresh README eval table. `change`/`fusion` stay zero-shot, honestly flagged in the trace.

---

## 8. Suggested slide outline (talking-point map)

1. **Problem & motivation** — §0 paragraph + three input regimes + five queries.
2. **The grading insight** — §1: observable execution trace > internal reasoning; ISRO unseen data ⇒ compatibility + honesty matter.
3. **Architecture** — controller diagram (CLAUDE.md §2) + registry of specialists behind `predict()`.
4. **No free lunch in RS** — generic VLM ≠ specialist: see §4 one-sentence defense; BigEarthNet adaptation is mandatory and shown (RSVQAxBEN run).
5. **Datasets** — §3 table (highlight BigEarthNet-MM for fusion + RSVQAxBEN bridging).
6. **Controller deep-dive** — validator checks (format/modality/co-registration/dates), registry, permitted params, confidence policy, audit JSON example (copy from a real run).
7. **Evaluation** — §5 protocol table with our numbers + timestamp.
8. **Build status / graded core demo** — §7b: controller round-trip, rejection behavior, audit JSON from a real run.
9. **GUI demo** — §1 flow: five queries, evidence overlays, audit panel, PDF report.
10. **Risks & honesty** — §6–7.
11. **References** — §4 table links.

---
*Maintainers: update "Status" cells and the eval table as the build progresses; copy audit-trace JSON examples from `runtime/traces/` into slide 6.*