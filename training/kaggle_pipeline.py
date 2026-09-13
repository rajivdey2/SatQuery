"""One-shot pipeline for training the rs_vlm specialist on Kaggle / Colab.

Paste this whole file as a notebook script (Kaggle: File -> Upload Notebook ->
Or New Notebook -> add as .py), or run directly on any GPU box.

Flow:
  1. RSVQAxBEN  -> SFT records (BigEarthNet-derived VQA; domain bootstrap)
  2. VRSBench   -> SFT records (optional: VQA/caption stage)
  3. QLoRA stage 1 (rsvqaxben) then stage 2 (vrsbench_vqa_cap, if present)
  4. Prints a zip path + the exact commands to wire the adapter into the API.

Notebook setup before running:
  * Upload the repo as a data source, or paste this file + pull the repo:
        git clone https://github.com/<your>/<repo>.git  /kaggle/working/satquery
  * Datasets (as Kaggle datasets or internet download):
        RSVQAxBEN  -> /kaggle/input/rsvqaxben           (unzipped)
        VRSBench   -> /kaggle/input/vrsbench            (optional; images + ann)
Arguments are env vars so a Kaggle notebook "kaggle_pipeline.py --envvariables" isn't needed:
  export SRC=/kaggle/working/satquery
  export RSVQAxBEN=/kaggle/input/rsvqaxben
  export VRSBENCH=/kaggle/input/vrsbench
  export VRSBENCH_ANN=/kaggle/input/vrsbench/vrsbench_qa.jsonl     # optional
  export OUT=/kaggle/working/satquery_out
  python $SRC/training/kaggle_pipeline.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(os.environ.get("SRC", "/kaggle/working/satquery"))
RSVQAXBEN = Path(os.environ.get("RSVQAXBEN", "/kaggle/input/rsvqaxben"))
VRSBENCH = Path(os.environ.get("VRSBENCH", ""))          # optional
VRSBENCH_ANN = Path(os.environ.get("VRSBENCH_ANN", ""))  # optional, jsonl
OUT = Path(os.environ.get("OUT", "/kaggle/working/satquery_out"))
MAX_SAMPLES = int(os.environ.get("MAX_SAMPLES", "0"))

CONFIG = SRC / "training" / "lora_configs" / "rs_vlm.yaml"
ADAPTERS = OUT / "adapters"


def run(cmd: str, **kw):
    print(f"\n>>> {cmd}", flush=True)
    return subprocess.run(cmd, shell=True, check=False, **kw)


def py(script: str, **kw):
    return run(f"python {script}", **kw)


def main() -> None:
    sys.path.insert(0, str(SRC))
    if not (CONFIG).exists():
        sys.exit(f"repo code missing at {SRC} (check the git clone path)")
    OUT.mkdir(parents=True, exist_ok=True)
    ADAPTERS.mkdir(parents=True, exist_ok=True)

    extra = f" --max_samples {MAX_SAMPLES}" if MAX_SAMPLES else ""

    # 1) RSVQAxBEN
    if not RSVQAXBEN.exists():
        print("\n[skip] RSVQAxBEN not found - nothing to train. See docs/PPT_CONTEXT.md for the Zenodo link.")
        return
    sft1 = OUT / "rsvqaxben_sft"
    if not (sft1 / "train" / "rsvqaxben.jsonl").exists():
        py(f"{SRC}/training/data_prep/rsvqaxben_to_sft.py --root {RSVQAXBEN} --out {sft1}")
    else:
        print(f"[keep] existing rsqaxben SFT at {sft1}")

    # 2) VRSBench (optional second stage)
    sft2 = OUT / "vrsbench_sft"
    have_vrsbench = VRSBENCH.exists() and VRSBENCH_ANN.exists()
    if have_vrsbench and not (sft2 / "train" / "vrsbench.jsonl").exists():
        py(f"{SRC}/training/data_prep/vrsbench_to_sft.py --root {VRSBENCH} --ann {VRSBENCH_ANN} --out {sft2}")

    # 3) QLoRA stage 1
    shutil.rmtree(str(ADAPTERS), ignore_errors=True)
    ADAPTERS.mkdir(parents=True, exist_ok=True)
    py(f"{SRC}/training/run_lora_train.py --config {CONFIG} --stage rsvqaxben "
       f"--data_dir {sft1} --output_dir {ADAPTERS}{extra}")

    # 4) QLoRA stage 2 (if VRSBench SFT was produced)
    if have_vrsbench and (sft2 / "train" / "vrsbench.jsonl").exists():
        py(f"{SRC}/training/run_lora_train.py --config {CONFIG} --stage vrsbench_vqa_cap "
           f"--data_dir {sft2} --adapter_path {ADAPTERS}/rsvqaxben --output_dir {ADAPTERS}{extra}")
        final = ADAPTERS / "vrsbench_vqa_cap"
    else:
        print("\n[info] VRSBench stage skipped - rsvqaxben adapter is the primary deliverable.")
        final = ADAPTERS / "rsvqaxben"

    # 5) zip + handoff instructions
    zip_dir = OUT / f"{final.name}.zip"
    run(f"cd {OUT} && zip -r {final.name}.zip {final.relative_to(OUT)}")
    print("\n================= HANDOFF =================")
    print(f"1. Download this zip inside Kaggle/Colab Files:  {zip_dir}")
    print(f"2. Unzip into the repo:  backend/runtime/adapters/rs_vlm/  (contains adapter_config.json + adapter_model.safetensors)")
    print("3. Run the API on a machine WITH a GPU (torch installed):")
    print("     pip install -r backend/requirements.txt -r training/requirements-gpu.txt")
    print('     set USE_REAL_MODEL=1')
    print('     set ADAPTER_RS_VLM=backend/runtime/adapters/rs_vlm')
    print("     python -m uvicorn backend.api.main:app --port 8000")
    print("   The audit trace per single-image VQA/caption/grounding will now report adapter=rs_vlm.")
    print("4. Refresh eval numbers (--backend vlm is required for the adapter to be used):")
    print("     python training/eval/run_benchmarks.py --dataset rsvqa --root <RSVQA-root> \\")
    print("         --backend vlm --adapter backend/runtime/adapters/rs_vlm")
    print("     python training/eval/run_benchmarks.py --dataset vrsbench_ground --root <VRSBENCH> \\")
    print("         --ann <vrsbench grounding jsonl> --backend vlm \\")
    print("         --adapter backend/runtime/adapters/rs_vlm")
    print("   Baseline column first (no GPU needed):  --backend analysis")


if __name__ == "__main__":
    main()