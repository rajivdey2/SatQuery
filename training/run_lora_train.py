"""QLoRA SFT trainer for the SatQuery specialists (runs on a GPU box / Kaggle / Colab).

Follows the official Qwen3-VL SFT recipe (transformers/examples vision) that
TRL supports: one LoRA adapter over a 4-bit base model, vision tower + projector
+ embeddings frozen, LoRA on LLM attention layers (rank 64 / alpha 128), one
epoch per stage.

Training data: JSONL produced by the data_prep converters, one record per line:
  {"messages":[{"role":"system","content":...},
               {"role":"user","content":<prompt>,"images":[<abs paths>]},
               {"role":"assistant","content":<answer>}]}

Usage:
  python run_lora_train.py --config lora_configs/rs_vlm.yaml --stage rsvqaxben \
      --data_dir /kaggle/input/rsvqaxben_sft --output_dir /kaggle/working/satquery_out
  # continue on the next stage reusing the previous adapter:
  python run_lora_train.py --config lora_configs/rs_vlm.yaml --stage vrsbench_vqa_cap \
      --data_dir /kaggle/input/vrsbench_sft \
      --adapter_path /kaggle/working/satquery_out/rsvqaxben \
      --output_dir /kaggle/working/satquery_out

Caveat: labels mask only pad tokens (matches the official recipe); LoRA rank 64
converges fine this way in ~1 epoch per stage.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--stage", required=True, help="id of the stage in the yaml to train on")
    ap.add_argument("--data_dir", required=True, help="dir containing train/<stage>.jsonl")
    ap.add_argument("--output_dir", required=True)
    ap.add_argument("--adapter_path", default="", help="previous stage adapter to continue from")
    ap.add_argument("--max_samples", type=int, default=0, help="0 = full set (hotfix/debug: set a small N)")
    a = ap.parse_args()

    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    stages = {s["name"]: s for s in cfg["stages"]}
    if a.stage not in stages:
        sys.exit(f"stage '{a.stage}' not in config (have: {list(stages)})")
    stage_cfg = stages[a.stage]
    tr = cfg["training"]
    peft_cfg = cfg.get("peft", {})
    quant = cfg.get("quantization", {})

    import torch
    from datasets import load_dataset
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from PIL import Image as PILImage
    from transformers import (AutoModelForVision2Seq, AutoProcessor,
                              BitsAndBytesConfig)

    data_root = Path(a.data_dir)
    jsonl = data_root / "train" / f"{a.stage}.jsonl"
    if not jsonl.exists():
        # Stage name may not match the file the converter wrote; pick any jsonl.
        candidates = sorted((data_root / "train").glob("*.jsonl"))
        if not candidates:
            sys.exit(f"training records missing under {data_root}/train (wanted {a.stage}.jsonl)")
        jsonl = candidates[0]
        print(f"[train] using records file: {jsonl}")

    print(f"[train] loading {cfg['model_id']} (4-bit) ...")
    if a.adapter_path and (a.stage == a.adapter_path.rstrip("/\\").split("/")[-1] or a.adapter_path.endswith(a.stage)):
        print("[train] NOTE: continuing from a previous stage adapter.")

    bnb = BitsAndBytesConfig(
        load_in_4bit=quant.get("load_in_4bit", True),
        bnb_4bit_quant_type=quant.get("bnb_4bit_quant_type", "nf4"),
        bnb_4bit_compute_dtype=getattr(torch, quant.get("bnb_4bit_compute_dtype", "float16")),
        bnb_4bit_use_double_quant=quant.get("bnb_4bit_use_double_quant", True),
    )
    model = AutoModelForVision2Seq.from_pretrained(cfg["model_id"], quantization_config=bnb,
                                                   torch_dtype=torch.float16, device_map="auto")
    processor = AutoProcessor.from_pretrained(cfg["model_id"])

    # Freeze the frozen parts BEFORE prepare_model_for_kbit_training so the
    # adapter only ever touches the LLM attention layers.
    if peft_cfg.get("freeze_vision", True):
        for p in model.visual.parameters():
            p.requires_grad = False
        print("[train] vision tower + projector frozen")
    if peft_cfg.get("freeze_embeddings", True):
        try:
            for p in model.model.embed_tokens.parameters():
                p.requires_grad = False
            print("[train] embeddings frozen")
        except AttributeError:
            pass
    model = prepare_model_for_kbit_training(model)

    ds = load_dataset("json", data_files=str(jsonl), split="train")
    if a.max_samples > 0:
        ds = ds.select(range(min(a.max_samples, len(ds))))

    # Deferred image opening: resolve to PIL now so the collator stays light.
    def _open(p: str) -> PILImage.Image:
        pp = Path(p)
        if not pp.is_absolute():
            pp = data_root / p
        return PILImage.open(pp).convert("RGB")

    def prepare(ex):
        msgs = []
        imgs: list = []
        for m in ex["messages"]:
            role = m.get("role")
            if m.get("images"):
                for ip in m["images"]:
                    try:
                        imgs.append(_open(ip))
                    except Exception:
                        continue
                content = [{"type": "image"} for _ in imgs] + [{"type": "text", "text": m.get("content", "")}]
            else:
                content = m.get("content", "")
            msgs.append({"role": role, "content": content})
        if not imgs:
            return None
        return {"messages": msgs, "images": imgs}

    ds = ds.map(prepare, remove_columns=ds.column_names)
    ds = ds.filter(lambda ex: ex is not None)
    print(f"[train] {len(ds)} vision-ready samples")

    def collate(features):
        msgs = [f["messages"] for f in features]
        images = [f["images"] for f in features]
        texts = [processor.apply_chat_template(m, tokenize=False, add_generation_prompt=False) for m in msgs]
        batch = processor(text=texts, images=images, padding=True, return_tensors="pt")
        labels = batch["input_ids"].clone()
        labels[labels == processor.tokenizer.pad_token_id] = -100
        batch["labels"] = labels
        return batch

    lora = LoraConfig(
        r=int(peft_cfg.get("lora_rank", 64)),
        lora_alpha=int(peft_cfg.get("lora_alpha", 128)),
        target_modules=peft_cfg.get("target_modules"),
        lora_dropout=peft_cfg.get("lora_dropout", 0.05),
        bias="none",
        task_type="CAUSAL_LM",
    )
    if a.adapter_path:
        from peft import PeftModel

        model = PeftModel.from_pretrained(model, a.adapter_path)
    else:
        model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    from trl import SFTConfig, SFTTrainer

    out = Path(a.output_dir) / a.stage
    sft = SFTConfig(
        output_dir=str(out),
        max_length=int(tr["max_seq_len"]),
        per_device_train_batch_size=int(tr["per_device_batch"]),
        gradient_accumulation_steps=int(tr["grad_accum"]),
        learning_rate=float(tr["lr"]),
        lr_scheduler_type=tr["lr_scheduler"],
        warmup_ratio=float(tr["warmup_ratio"]),
        num_train_epochs=float(stage_cfg.get("epochs", tr.get("epochs", 1))),
        logging_steps=int(tr["logging_steps"]),
        save_steps=int(tr["save_steps"]),
        save_total_limit=2,
        bf16=bool(tr.get("bf16", False)),
        fp16=bool(tr.get("fp16", True)),
        remove_unused_columns=False,
        report_to=["none"],
    )
    trainer = SFTTrainer(
        model=model,
        args=sft,
        processing_class=processor,
        train_dataset=ds,
        data_collator=collate,
    )
    trainer.train()
    trainer.save_model(str(out))
    processor.save_pretrained(str(out))
    print("[train] DONE")
    print(f"[train] adapter -> {out}")
    print(f"[train] zip it for download:  cd {a.output_dir} && zip -r {a.stage}.zip {a.stage}")


if __name__ == "__main__":
    main()