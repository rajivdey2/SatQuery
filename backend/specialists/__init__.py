"""The three problem-statement specialists behind one interface.

  * ``rs_vlm``  — single-image VQA (mandatory), scene description, text-guided grounding
  * ``change``  — bi-temporal change-VQA and change description (mandatory), plus change map
  * ``fusion``  — co-registered optical+SAR joint extraction (mandatory)

``specialist_for_task`` is the only entry point the controller uses; the registry
in ``controller/registry.py`` decides which task maps to which specialist and which
parameters are permitted to reach it.
"""
from __future__ import annotations

from typing import Dict, List

from backend.specialists.base import Specialist, SpecialistResult
from backend.specialists.change import ChangeSpecialist
from backend.specialists.change import TASKS as CHANGE_TASKS
from backend.specialists.fusion import TASKS as FUSION_TASKS
from backend.specialists.fusion import FusionSpecialist
from backend.specialists.rs_vlm import TASKS as RS_VLM_TASKS
from backend.specialists.rs_vlm import RsVlmSpecialist

_FACTORIES = {}
for _t in RS_VLM_TASKS:
    _FACTORIES[_t] = RsVlmSpecialist
for _t in CHANGE_TASKS:
    _FACTORIES[_t] = ChangeSpecialist
for _t in FUSION_TASKS:
    _FACTORIES[_t] = FusionSpecialist

ALL_TASKS: List[str] = list(_FACTORIES)


def specialist_for_task(task: str) -> Specialist:
    """Instantiate the specialist that owns ``task``."""
    factory = _FACTORIES.get(task)
    if factory is None:
        raise KeyError(f"no specialist registered for task '{task}'")
    return factory(task)


def describe_all() -> Dict[str, dict]:
    """Registry-facing description of every specialist (served by /api/registry)."""
    return {task: specialist_for_task(task).describe() for task in ALL_TASKS}


__all__ = ["Specialist", "SpecialistResult", "specialist_for_task", "describe_all", "ALL_TASKS"]
