# SatQuery AI — architecture

> Problem statement 26167 (ISRO/SAC). The graded artefact is the **observable
> execution trace**: the selected task, the models/tools chosen from a predefined
> registry, the permitted parameters, the outputs and the confidence. Everything
> below is arranged around producing that trace honestly.

## 1. Request path

```
 user query + 1–2 images
          │
          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          AGENTIC CONTROLLER                                 │
│                                                                             │
│ 1. Input Validator          controller/validator.py                         │
│      format · modality (metadata first, statistics only as fallback) ·      │
│      georeferencing · co-registration (CRS-aware) · acquisition dates       │
│      → rejects and explains; never silently proceeds                        │
│                                                                             │
│ 2. Task Classifier          controller/classifier.py                        │
│      scored rules over query wording × input configuration                  │
│      → one of six tasks, with runner-up scores and infeasible tasks logged   │
│                                                                             │
│ 3. Router                   controller/registry.py                          │
│      predefined registry: task → tool, required inputs, typed parameter      │
│      schema. Unpermitted or out-of-range parameters are rejected and named   │
│                                                                             │
│ 4. Executor                 controller/executor.py                          │
│      builds a plan (one entry, or two when the query asks for something the  │
│      first entry does not produce), runs it, records every tool step         │
│                                                                             │
│ 5. Combiner + Confidence    controller/combiner.py · controller/confidence.py│
│      merges text with boxes/masks/overlays; composes confidence only from    │
│      measured signals, otherwise reports not_available                       │
│                                                                             │
│ 6. Audit Trace              controller/audit.py                             │
│      schema 2.0.0 → runtime/traces/<run_id>.json, rendered in the GUI and    │
│      embedded verbatim in the PDF report                                    │
└───────────────┬─────────────────────┬──────────────────────┬────────────────┘
                │                     │                      │
                ▼                     ▼                      ▼
    ┌───────────────────┐  ┌────────────────────┐  ┌──────────────────────┐
    │ specialists/      │  │ specialists/       │  │ specialists/         │
    │ rs_vlm.py         │  │ change.py          │  │ fusion.py            │
    │                   │  │                    │  │                      │
    │ single_vqa        │  │ change_vqa         │  │ sar_optical_fusion   │
    │ single_caption    │  │ change_description │  │                      │
    │ single_grounding  │  │ + change map       │  │ + agreement + joint  │
    └─────────┬─────────┘  └─────────┬──────────┘  └──────────┬───────────┘
              └──────────────┬───────┴────────────────────────┘
                             ▼
                  ┌──────────────────────────┐
                  │  analysis/  (measure)    │
                  │  indices · thresholds ·  │
                  │  landcover · regions ·   │
                  │  change · fusion ·       │
                  │  grounding · render      │
                  │  scene_labels (adapted)  │
                  └────────────┬─────────────┘
                               ▼
                  ┌──────────────────────────┐
                  │  narrate  (speak)        │
                  │  templates, or Qwen3-VL  │
                  │  constrained to the      │
                  │  measured numbers        │
                  └──────────────────────────┘
```

## 2. The measure-then-speak split

Every specialist follows the same two-phase contract:

1. **Measure.** Raw bands → indices → thresholds with a recorded separability →
   class masks → connected regions → per-class extents in hectares. All of it lands
   in the trace's `measurements` block as typed records.
2. **Speak.** The narration layer may only restate measured values. With no VLM
   present, templates do it. With a VLM present, Qwen3-VL is handed the image *and*
   the measurement record and asked to rephrase; the numbers are fixed.

Turning the VLM off therefore costs fluency, not correctness — and every number in
an answer can be traced to a threshold and a pixel count.

## 3. Data model

| Stage | Type | Where |
|---|---|---|
| Probe result | `ImageInfo` | `preprocessing/geotiff_io.py` |
| Band roles | `BandMap` | `preprocessing/bands.py` |
| Calibrated SAR | `SarChannel` (dB, ENL, convention, calibrated) | `preprocessing/sar_ops.py` |
| Analysis-ready image | `PreparedImage` (raw bands + valid mask + pixel area) | `preprocessing/pipeline.py` |
| Index stack | `IndexStack` (values + why the rest are unavailable) | `analysis/indices.py` |
| Measurement | `LandCoverMeasurement` / `ChangeMeasurement` / `FusionMeasurement` / `GroundingMeasurement` | `analysis/measurements.py` |
| Specialist output | `SpecialistResult` | `specialists/base.py` |
| Graded trace | `AuditTrace` | `controller/audit.py` |

## 4. Input configurations and their gates

| Configuration | Detected by | Tasks it enables | Hard gate |
|---|---|---|---|
| single image | one file | `single_vqa`, `single_caption`, `single_grounding` | — |
| bi-temporal pair | two files, same modality | `change_vqa`, `change_description` | dates must differ where known; co-registration must be verifiable when both are georeferenced |
| cross-modal pair | two files, one SAR + one optical | `sar_optical_fusion` | co-registration must be verifiable |

A change task cannot win on a single image and fusion cannot win without a SAR
image, whatever the wording says — the classifier asks the registry, not the query.

## 5. Confidence sources

| Source | Signal | Where it comes from |
|---|---|---|
| `measurement_composite` | Otsu separability, band completeness, valid-pixel fraction, change-threshold headroom, region quality | `analysis/thresholds.py`, `analysis/*` |
| `cross_modal_agreement` | Cohen's kappa between optical and SAR evidence | `analysis/fusion.py` |
| `learned_probability` | top-2 margin of the adapted BigEarthNet-MM head | `analysis/scene_labels.py` |
| `logit_margin` | mean top-2 token probability margin | `specialists/backends/model_loader.py` |
| `self_consistency` | agreement across sampled narrations | `specialists/backends/vlm.py` |
| `validator_gate` | deterministic rejection | `controller/validator.py` |
| `not_available` | nothing measurable | reported instead of a guess |

`calibrated` is always `false` and the note says so: the composite is a documented
combination of measured quantities, not a probability fitted against labelled
reliability data.

## 6. Remote-sensing adaptation

Two independent components, both optional to run but both real:

* **`training/adapt_ben_mm.py`** — dual-branch (Sentinel-1 + Sentinel-2) multi-label
  land-cover head over the BigEarthNet 19-class nomenclature. NumPy, CPU, minutes.
  Its feature extractor is imported from the serving code, and inference refuses a
  head trained on a different feature spec. This is the mandatory
  BigEarthNet adaptation.
* **`training/run_lora_train.py`** — 4-bit QLoRA on Qwen3-VL for the narration
  model, staged RSVQAxBEN → VRSBench → RSVG (single image), CDVQA + LEVIR-CC
  (change), BigEarthNet-MM (fusion). GPU, optional, upgrades wording only.

## 7. Failure behaviour

| Situation | Behaviour |
|---|---|
| Footprints overlap < 50%, or GSD ratio > 8× | rejected, with the measured overlap in the trace |
| Same acquisition date on a bi-temporal pair | rejected |
| No CRS | accepted; areas in pixels only, spatial words switch from compass to image orientation, confidence penalised |
| No NIR band (RGB benchmark PNG) | accepted; NDVI/NDWI reported unavailable, visible proxies used, reliability `medium` |
| Single-pol SAR | accepted; vegetation explicitly *not* claimed |
| Uncalibrated SAR digital numbers | accepted; level thresholds derived from the scene, stated in the trace |
| Specialist raises | trace written with the failed step and `error`; the API returns a job in state `error`, never a bare 500 |
