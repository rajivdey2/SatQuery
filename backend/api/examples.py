"""Pre-computed showcase examples.

A live demo has two failure modes that have nothing to do with the system being
wrong: the first run is slow because nothing is warm, and a judge clicking around
hits an empty dashboard. So the five representative queries from the problem
statement are executed once, offline, by ``scripts/build_examples.py`` and stored
here as complete job records -- answer, measurements, rendered evidence and the
full audit trace.

The stored records use exactly the same shape as ``/api/jobs/{id}``, so the GUI
renders them through the same components with no special-casing: an example is
indistinguishable from a live run except that it is instant. Every example also
keeps the query and the input files, so "run this live" reproduces it in front of
the audience.

Nothing here fabricates output. If the examples have not been baked, the endpoint
says so and the GUI falls back to the live demo buttons.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from backend.config import EXAMPLE_DIR, OUTPUT_DIR

INDEX_NAME = "index.json"


def _index_path() -> Path:
    return Path(EXAMPLE_DIR) / INDEX_NAME


def available() -> bool:
    return _index_path().exists()


def _evidence_present(record: dict) -> bool:
    """True when every rendered artefact the record references still exists."""
    items = ((record.get("trace") or {}).get("outputs") or {}).get("evidence") or []
    for item in items:
        name = Path(str(item.get("path", ""))).name
        if name and not (Path(OUTPUT_DIR) / name).exists():
            return False
    return True


def load_record(slug: str) -> Optional[dict]:
    path = Path(EXAMPLE_DIR) / f"{Path(slug).name}.json"
    if not path.exists() or path.name == INDEX_NAME:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_index() -> dict:
    """Index of baked examples, with a per-example freshness check."""
    path = _index_path()
    if not path.exists():
        return {"available": False, "examples": [],
                "hint": "Run `python scripts/build_examples.py` to bake the showcase examples."}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"available": False, "examples": [],
                "hint": f"Example index is unreadable ({exc}); re-run scripts/build_examples.py."}

    entries: List[dict] = []
    for entry in payload.get("examples", []):
        record = load_record(entry.get("slug", ""))
        entry = dict(entry)
        entry["loadable"] = record is not None
        entry["evidence_ok"] = bool(record and _evidence_present(record))
        entries.append(entry)
    payload["examples"] = entries
    payload["available"] = any(e["loadable"] for e in entries)
    stale = [e["slug"] for e in entries if not e["evidence_ok"]]
    if stale:
        payload["hint"] = (f"Rendered evidence is missing for {', '.join(stale)}; "
                           "re-run `python scripts/build_examples.py`.")
    return payload


def as_job(record: dict) -> dict:
    """Return the record in the job shape the GUI already knows how to render."""
    job = {k: v for k, v in record.items() if k != "example"}
    job.setdefault("status", "done")
    job["is_example"] = True
    job["example"] = record.get("example", {})
    return job
