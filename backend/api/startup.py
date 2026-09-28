"""Startup self-heal: the deployed demo fixes itself without manual steps.

The Showcase examples are baked records, but their rendered evidence PNGs live
in gitignored ``backend/runtime/outputs/`` — they only exist on disk if
``scripts/build_examples.py`` ran (the Render ``buildCommand``) on that
machine. If a deploy skipped or failed that step, the Showcase would show
placeholder thumbnails forever with no recovery path.

So on boot the app spawns one daemon thread that regenerates whatever is
missing: demo inputs first, then any example whose evidence is gone. It never
blocks startup or ``/health`` — Render's health check passes immediately and
the Showcase fills in a minute or two later (refresh the page). Live analysis
runs work from second one regardless, since they render fresh evidence.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
import threading
from pathlib import Path

log = logging.getLogger("satquery.startup")


def _load_script(name: str):
    """Import ``scripts/<name>.py`` by path (scripts/ is not a package)."""
    from backend.config import PROJECT_DIR

    path = Path(PROJECT_DIR) / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_satquery_script_{name}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - missing file
        raise ImportError(f"Cannot load scripts/{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def ensure_demo_ready() -> dict:
    """Regenerate missing demo inputs / example evidence. Returns what it did."""
    from backend.api import examples as store
    from backend.config import DEMO_DIR

    report: dict = {"demo_data": "ok", "examples": "ok"}

    if not any(Path(DEMO_DIR).glob("*.tif")):
        log.warning("demo inputs missing — generating via scripts/make_demo_data.py")
        _load_script("make_demo_data").main()
        report["demo_data"] = "regenerated"

    index = store.load_index()
    entries = index.get("examples", [])
    stale = [e.get("slug") for e in entries if not e.get("evidence_ok", False)]
    if not index.get("available") or stale:
        log.warning("example evidence missing for %s — rebaking via scripts/build_examples.py",
                    stale or ["all"])
        build = _load_script("build_examples")
        argv, sys.argv = sys.argv, ["build_examples.py"]  # main() parses argv
        try:
            rc = build.main()
        finally:
            sys.argv = argv
        report["examples"] = "rebaked" if rc == 0 else f"rebake_failed(rc={rc})"
    return report


def _heal_in_background() -> None:
    try:
        report = ensure_demo_ready()
        if any(v not in ("ok",) for v in report.values()):
            log.warning("startup demo heal finished: %s", report)
        else:
            log.info("startup demo heal: everything already present")
    except Exception as exc:  # never take the server down over demo cosmetics
        log.warning("startup demo heal failed (live runs still work): %s: %s",
                    type(exc).__name__, exc)


def schedule_startup_heal() -> None:
    thread = threading.Thread(target=_heal_in_background,
                              name="satquery-startup-heal", daemon=True)
    thread.start()
