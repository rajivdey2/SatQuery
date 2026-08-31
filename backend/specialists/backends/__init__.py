"""Interchangeable inference backends behind the specialists.

``model_loader`` loads Qwen3-VL (optionally 4-bit, optionally with a LoRA adapter
from ``training/``) and exposes a real top-2 logit margin. ``vlm`` narrates the
measurement records with that model. ``mock`` is the explicitly-labelled
placeholder used only when ``SATQUERY_MOCK=1``.

Specialists select a backend at call time and record which one ran in the audit
trace, so the same deployment can go from measurement-only to adapter-backed
without a code change.
"""
