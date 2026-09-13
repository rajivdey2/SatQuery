# Model training and adaptation — instructions

> **Read this first:** the application is fully functional *before any training
> runs*. The measurement engine (spectral/radar indices, thresholds, change vector
> analysis, cross-modal agreement) needs no weights and no GPU, which is why the
> demo works on a laptop. Training adds two independent, clearly-scoped upgrades:
>
> | | What it is | Satisfies | Hardware | Time |
> |---|---|---|---|---|
> | **A. Adapted land-cover head** | dual-branch Sentinel-1 + Sentinel-2 multi-label classifier trained on BigEarthNet-MM | the **mandatory** "at least one visual component adapted using BigEarthNet" | CPU (laptop) | 10–90 min incl. feature extraction |
> | **B. QLoRA narration adapters** | 4-bit LoRA on Qwen3-VL so the *wording* of answers is remote-sensing native | optional polish + benchmark numbers | 1 GPU ≥16 GB (Kaggle/Colab free tier) | 2–5 h per stage |
>
> Do **A** first. It is the requirement, it is cheap, and it is verifiable. **B** is
> a wording upgrade — every number in an answer comes from the measurement engine
> either way, so a failed LoRA run never makes the system wrong, only less fluent.

---

## 0. Prerequisites

```bash
cd C:\project\satquery
python -m venv venv && venv\Scripts\activate      # Windows
pip install -r backend/requirements.txt
```

Verify the app runs before training anything:

```bash
python scripts/make_demo_data.py       # synthetic scenes with known ground truth
python scripts/smoke_controller.py     # all five problem-statement queries
python -m pytest tests -q              # measurement + controller + API tests
```

---

## A. Adapted BigEarthNet-MM land-cover head (mandatory, CPU)

### A.1 What gets trained

`training/adapt_ben_mm.py` trains a small multi-label classifier over the
**BigEarthNet 19-class nomenclature**:

```
Sentinel-2 optical branch → NDVI/NDWI/MNDWI/NDBI/brightness/texture statistics
Sentinel-1 SAR branch     → backscatter dB + roughness + polarimetric ratio statistics
                          ↓
        standardise → MLP(dim → 96 → 19) → sigmoid, positive-weighted BCE, Adam
                          ↓
              models/ben_mm_lc.npz  +  models/ben_mm_lc.metrics.json
```

Two design points worth stating to a reviewer:

- **The feature extractor is imported from the serving code**
  (`backend/analysis/scene_labels.extract_features`), not reimplemented. Train-time
  and inference-time features are therefore identical by construction, and
  inference *refuses to load* a head whose feature spec differs.
- **Metrics are held-out.** Per-class thresholds are tuned on a validation split and
  the reported micro/macro-F1 and mAP come from that split, not from training data.

### A.2 Prove the trainer works before downloading 60 GB

```bash
python training/adapt_ben_mm.py --self-test
```

Trains on synthetic features with known structure and asserts the head beats a
constant-prior baseline. Exits 0 on success. Do this first — it separates "my
trainer is broken" from "my data path is wrong".

### A.3 Get the data

Any **one** of these layouts works; the loader auto-detects which you have.

**Option 1 — BigEarthNet v1 (simplest to obtain)**

- Sentinel-2: <https://bigearth.net> → `BigEarthNet-S2-v1.0.tar.gz` (~66 GB)
- Sentinel-1: `BigEarthNet-S1-v1.0.tar.gz` (~55 GB)

```
<root>/BigEarthNet-v1.0/<patch>/<patch>_B02.tif …_B12.tif
<root>/BigEarthNet-v1.0/<patch>/<patch>_labels_metadata.json
<root>/BigEarthNet-S1-v1.0/<s1_patch>/<s1_patch>_VV.tif, _VH.tif
<root>/BigEarthNet-S1-v1.0/<s1_patch>/<s1_patch>_labels_metadata.json   ← carries
                                                    "corresponding_s2_patch"
```

**Option 2 — reBEN / BigEarthNet v2.0 (cleaner labels, recommended if bandwidth allows)**

<https://bigearth.net> or Zenodo (arXiv:2407.03653). Needs `pandas` for the
parquet metadata: `pip install pandas pyarrow`.

**Option 3 — prepared/stacked (use this to train on a small subset)**

```
<root>/patches/<id>.tif      # 12-band S2 stack, band order B01..B09,B11,B12
<root>/s1/<id>_S1.tif        # 2-band S1 stack, VV then VH
<root>/labels.jsonl          # {"patch": "<id>", "labels": ["Inland waters", ...]}
```

> **Low-bandwidth route:** you do not need the full archive. 5,000–20,000 patches
> are enough for a defensible head. Download one or two of the S2 tar shards, take
> the matching S1 patches, and pass `--limit`.

### A.4 Train

```bash
# 1) extract features once and cache them (this is the slow part: raster I/O)
python training/adapt_ben_mm.py --root D:\data\BigEarthNet --limit 20000 \
    --cache work/ben_feats.npz

# 2) iterate on the head in seconds from the cache
python training/adapt_ben_mm.py --features work/ben_feats.npz \
    --out models/ben_mm_lc.npz --hidden 96 --epochs 60 --lr 3e-3

# one-shot alternative
python training/adapt_ben_mm.py --root D:\data\BigEarthNet --out models/ben_mm_lc.npz
```

Useful flags: `--limit N` (cap patches), `--hidden`, `--epochs`, `--lr`, `--batch`,
`--val-fraction`, `--seed`.

Expect roughly (real BigEarthNet, ~20k patches, held-out split):

| metric | ballpark |
|---|---|
| micro-F1 | 0.55 – 0.70 |
| macro-F1 | 0.30 – 0.50 |
| mAP | 0.45 – 0.65 |

These are statistics-of-index features, not a CNN — the point is a *real, honest,
reproducible* adaptation on BigEarthNet with held-out numbers, not a leaderboard
score. Record whatever you actually get; `models/ben_mm_lc.metrics.json` has the
per-class table.

### A.5 Wire it in and verify

The backend picks it up automatically from `models/ben_mm_lc.npz`. To confirm:

```bash
python -c "from backend.analysis import scene_labels; print(scene_labels.describe())"
# {'available': True, 'model_id': 'ben_mm_lc_v1', 'classes': 19, 'features': 60, ...}
```

Then start the API and check `GET /health` → `adapted_head.available: true`. In the
GUI the header shows **adapted head loaded**, the measurement panel grows an
*Adapted BigEarthNet-MM head* section with per-class probabilities, and the audit
trace gains a `learned_margin` confidence component.

Override the path with `BEN_HEAD_PATH`, or disable with
`SATQUERY_USE_BEN_HEAD=0`.

---

## B. QLoRA narration adapters on Qwen3-VL (optional, GPU)

### B.1 Where to run

Kaggle (2× T4, 30 h/week free) or Colab T4. Install:

```bash
pip install -r training/requirements-gpu.txt
```

4-bit NF4 quantisation of Qwen3-VL-4B fits a 16 GB T4 with rank-64 LoRA at
`per_device_batch: 2, grad_accum: 8`. If you OOM: drop `max_seq_len` to 2048, then
`max_pixels` to 1048576, then `per_device_batch` to 1.

### B.2 Convert each dataset to the SFT format

One converter per dataset, all emitting the same JSONL
(`{"messages":[system, user+images, assistant]}`):

```bash
# single-image: BigEarthNet-derived VQA → domain bootstrap
python training/data_prep/rsvqaxben_to_sft.py --root <rsvqaxben> --out data/sft/rsvqaxben

# single-image: VQA + captioning + grounding
python training/data_prep/vrsbench_to_sft.py --root <vrsbench_images> \
    --ann <vrsbench_annotations>.jsonl --out data/sft/vrsbench

# grounding: referring expression → box in 0..100 coordinates
python training/data_prep/rsvg_to_sft.py --root <rsvg_root> --out data/sft/rsvg

# change: multiple-choice change VQA (mandatory eval target)
python training/data_prep/cdvqa_to_sft.py --root <cdvqa_root> \
    --ann cdvqa_qa.json --out data/sft/cdvqa

# change: free-text change captions (+ measured extent when masks are present)
python training/data_prep/levir_cc_to_sft.py --root <levir_cc_root> \
    --out data/sft/levir_cc --with-mask-extent

# fusion: BigEarthNet-MM optical+SAR pairs, CORINE labels as weak supervision
python training/data_prep/ben_mm_to_sft.py --root <ben_mm_root> --out data/sft/ben_mm
```

Dataset sources: RSVQAxBEN and RSVQA `rsvqa.sylvainlobry.com` · VRSBench
`vrsbench.github.io` · RSVG (DIOR-based) GitHub · CDVQA `arXiv:2112.06343` ·
LEVIR-CC / LEVIR-CD GitHub · BigEarthNet `bigearth.net`.

### B.3 Train, one stage at a time

Order matters — domain-adapt first, then task-specialise. Each stage continues from
the previous stage's adapter.

```bash
# --- single-image specialist (rs_vlm) ---
python training/run_lora_train.py --config training/lora_configs/rs_vlm.yaml \
    --stage rsvqaxben --data_dir data/sft/rsvqaxben --output_dir runs/rs_vlm

python training/run_lora_train.py --config training/lora_configs/rs_vlm.yaml \
    --stage vrsbench_vqa_cap --data_dir data/sft/vrsbench \
    --adapter_path runs/rs_vlm/rsvqaxben --output_dir runs/rs_vlm

python training/run_lora_train.py --config training/lora_configs/rs_vlm.yaml \
    --stage rsvg_grounding --data_dir data/sft/rsvg \
    --adapter_path runs/rs_vlm/vrsbench_vqa_cap --output_dir runs/rs_vlm

# --- change specialist ---
python training/run_lora_train.py --config training/lora_configs/change.yaml \
    --stage cdvqa --data_dir data/sft/cdvqa --output_dir runs/change
python training/run_lora_train.py --config training/lora_configs/change.yaml \
    --stage levir_cc --data_dir data/sft/levir_cc \
    --adapter_path runs/change/cdvqa --output_dir runs/change

# --- fusion specialist ---
python training/run_lora_train.py --config training/lora_configs/fusion.yaml \
    --stage ben_mm --data_dir data/sft/ben_mm --output_dir runs/fusion
```

Smoke-test any stage with `--max_samples 200` before spending GPU hours.

Config knobs live in `training/lora_configs/*.yaml`: rank 64 / alpha 128, LoRA on
`q,k,v,o` projections only, vision tower and embeddings frozen, 1 epoch per stage,
`lr 2e-4` cosine.

### B.4 Wire the adapters in

Download the adapter directory from the notebook, place it locally, then:

```powershell
$env:USE_REAL_MODEL = "1"
$env:MODEL_ID       = "Qwen/Qwen3-VL-4B-Instruct"
$env:ADAPTER_RS_VLM = "C:\project\satquery\models\rs_vlm_adapter"
$env:ADAPTER_CHANGE = "C:\project\satquery\models\change_adapter"
$env:ADAPTER_FUSION = "C:\project\satquery\models\fusion_adapter"
uvicorn backend.api.main:app --port 8000
```

What changes: `narration_source` in the audit trace goes from `measurement` to
`vlm+measurement`, the registry entry records the adapter name, and `logit_margin`
(and `self_consistency` if you set `SATQUERY_SELF_CONSISTENCY=3`) appear as
confidence components. What does **not** change: the numbers, which still come from
the measurement engine — the VLM is asked to rephrase them, not to compute them.

---

## C. Evaluation — filling the README table

Two backends, one protocol. Run the `analysis` backend first: it needs no GPU, so
the table is never empty.

```bash
# measurement engine (no GPU) — establishes the baseline column
python training/eval/run_benchmarks.py --dataset rsvqa --root <rsvqa_root> \
    --backend analysis --max-n 1000
python training/eval/run_benchmarks.py --dataset cdvqa --root <cdvqa_root> \
    --ann cdvqa_qa.json --backend analysis
python training/eval/run_benchmarks.py --dataset vrsbench_ground --root <images> \
    --ann <ann>.jsonl --backend analysis

# same splits, adapter-backed narration
python training/eval/run_benchmarks.py --dataset vrsbench_vqa --root <images> \
    --ann <ann>.jsonl --backend vlm --model Qwen/Qwen3-VL-4B-Instruct \
    --adapter runs/rs_vlm/rsvg_grounding
```

Results land in `training/eval/results/<dataset>_<backend>_<epoch>.json`. Copy the
headline numbers into the README table **with the timestamp**, and cite the run
file. Metrics follow each benchmark's own protocol: accuracy with a per-type
breakdown for RSVQA, Acc@0.5 / Acc@0.7 on IoU for VRSBench grounding, per-type
accuracy plus AA and OA for each CDVQA test split.

Captioning (BLEU-4/METEOR/CIDEr/ROUGE-L) needs the official `pycocoevalcap`
scorers; `--dataset vrsbench_caption` writes the predictions array for you to score
with it rather than reporting an approximate number.

---

## D. Order of work if time is short

1. `--self-test` the head trainer. *(minutes)*
2. Train the head on a BigEarthNet subset. **This is the mandatory requirement.** *(1 evening)*
3. Run the `analysis` backend over RSVQA + CDVQA and fill the README table. *(hours, no GPU)*
4. One QLoRA stage: `rsvqaxben` → `vrsbench_vqa_cap` for `rs_vlm`. *(1 GPU session)*
5. Re-run the eval with `--backend vlm --adapter …` and add the second column.
6. Only then consider the change and fusion adapters.

Anything not trained stays honestly labelled in the audit trace as
`narration_source: measurement` with no adapter — which is a defensible state, not
a gap to hide.

---

## E. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `no BigEarthNet patches found under <root>` | layout not recognised | check the three layouts in §A.3; the error message lists them |
| `refusing <head>.npz: trained on a different feature spec` | head predates a change to `extract_features` | retrain (`--features` cache is still valid only if the spec is unchanged — otherwise re-extract) |
| `reBEN parquet metadata found but pandas is not installed` | missing dep | `pip install pandas pyarrow` |
| head trains but micro-F1 ≈ 0 | labels not matching the nomenclature | check `encode_labels` against your label strings; v1 uses 43 CORINE classes, this head uses the 19-class set |
| CUDA OOM in `run_lora_train.py` | sequence/pixel budget | `max_seq_len` 2048 → `max_pixels` 1048576 → `per_device_batch` 1 |
| `stage 'x' not in config` | stage name mismatch | stage names come from the YAML `stages[].name` |
| adapter loads but answers unchanged | `USE_REAL_MODEL` not set | narration only runs when `USE_REAL_MODEL=1` *and* torch+transformers import |
| eval accuracy suspiciously low | verbose answers vs short references | that is expected for the `analysis` backend on categorical benchmarks; `short_answer()` normalisation is applied identically to every prediction — report it as-is |
