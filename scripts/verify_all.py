"""One command that checks the whole system end to end.

Run this before a demo or after pulling changes. Each stage prints PASS/FAIL and the
script exits non-zero if anything failed, so it is also usable in CI.

    python scripts/verify_all.py                # everything
    python scripts/verify_all.py --quick        # skip pytest and the API smoke
    python scripts/verify_all.py --demo-ready   # only what a demo needs

Stages
  1. imports          every backend module imports cleanly
  2. compile          every .py byte-compiles
  3. demo data        synthetic scenes with known ground truth exist (generated if not)
  4. controller       the five problem-statement queries route correctly
  5. examples         the dashboard's showcase examples are baked and loadable
  6. api              HTTP surface, in-process: analyze -> job -> trace -> PDF
  7. tests            pytest
  8. adaptation       reports whether the BigEarthNet-MM head is trained
"""
from __future__ import annotations

import argparse
import compileall
import importlib
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = sys.executable

BACKEND_MODULES = [
    "backend.config",
    "backend.preprocessing.bands",
    "backend.preprocessing.geotiff_io",
    "backend.preprocessing.sar_ops",
    "backend.preprocessing.pipeline",
    "backend.analysis.measurements",
    "backend.analysis.thresholds",
    "backend.analysis.indices",
    "backend.analysis.regions",
    "backend.analysis.landcover",
    "backend.analysis.change",
    "backend.analysis.fusion",
    "backend.analysis.grounding",
    "backend.analysis.narrate",
    "backend.analysis.render",
    "backend.analysis.scene_labels",
    "backend.controller.audit",
    "backend.controller.validator",
    "backend.controller.classifier",
    "backend.controller.registry",
    "backend.controller.confidence",
    "backend.controller.combiner",
    "backend.controller.executor",
    "backend.specialists",
    "backend.specialists.rs_vlm",
    "backend.specialists.change",
    "backend.specialists.fusion",
    "backend.specialists.narration",
    "backend.specialists.backends.model_loader",
    "backend.specialists.backends.vlm",
    "backend.specialists.backends.mock",
    "backend.reports.pdf_report",
    "backend.api.examples",
    "backend.api.jobs",
    "backend.api.main",
]

RESULTS: list[tuple[str, bool, str]] = []


def record(stage: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((stage, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {stage}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def banner(text: str) -> None:
    print(f"\n{text}\n{'-' * len(text)}", flush=True)


def stage_imports() -> bool:
    banner("1. imports")
    ok = True
    for name in BACKEND_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:
            ok = record(name, False, f"{type(exc).__name__}: {exc}") and ok
    if ok:
        record(f"{len(BACKEND_MODULES)} backend modules", True)
    return ok


def stage_compile() -> bool:
    banner("2. byte-compile")
    ok = True
    for directory in ("backend", "training", "scripts", "tests"):
        path = ROOT / directory
        if not path.exists():
            continue
        result = compileall.compile_dir(str(path), quiet=1, force=False)
        ok = record(directory, bool(result)) and ok
    return ok


def stage_demo_data(generate: bool = True) -> bool:
    banner("3. demo data")
    from backend.config import DEMO_DIR

    demo = Path(DEMO_DIR)
    needed = ["areaA_20230101.tif", "areaA_20230701.tif", "areaB_optical.tif",
              "areaB_sar.tif", "areaD_mismatch.tif", "optical_city.png", "ground_truth.json"]
    missing = [n for n in needed if not (demo / n).exists()]
    if missing and generate:
        record("generating", True, f"missing {len(missing)} file(s)")
        result = subprocess.run([PY, str(ROOT / "scripts" / "make_demo_data.py")],
                                capture_output=True, text=True)
        if result.returncode != 0:
            return record("make_demo_data.py", False, result.stderr.strip()[-300:])
        missing = [n for n in needed if not (demo / n).exists()]
    if missing:
        return record("inputs present", False, f"still missing: {', '.join(missing)}")
    return record("inputs present", True, f"{len(needed)} files in {demo}")


def stage_controller() -> bool:
    banner("4. controller — the five problem-statement queries")
    result = subprocess.run([PY, str(ROOT / "scripts" / "smoke_controller.py")],
                            capture_output=True, text=True)
    tail = "\n".join(result.stdout.strip().splitlines()[-3:])
    print("\n".join("      " + line for line in result.stdout.strip().splitlines()))
    return record("smoke_controller.py", result.returncode == 0,
                  "" if result.returncode == 0 else tail or result.stderr.strip()[-300:])


def stage_examples(build: bool = True) -> bool:
    banner("5. showcase examples")
    from backend.api import examples as store

    index = store.load_index()
    if not index.get("available") and build:
        record("baking", True, "not baked yet")
        result = subprocess.run([PY, str(ROOT / "scripts" / "build_examples.py")],
                                capture_output=True, text=True)
        print("\n".join("      " + line for line in result.stdout.strip().splitlines()))
        if result.returncode != 0:
            record("build_examples.py", False, result.stderr.strip()[-300:])
        importlib.reload(store)
        index = store.load_index()

    if not index.get("available"):
        return record("examples available", False, index.get("hint", ""))
    entries = index["examples"]
    broken = [e["slug"] for e in entries if not e["loadable"] or not e["evidence_ok"]]
    mismatched = [e["slug"] for e in entries if not e.get("matched", True)]
    ok = record(f"{len(entries)} examples baked", not broken,
                f"broken: {', '.join(broken)}" if broken else "")
    if mismatched:
        ok = record("routed as expected", False, f"mismatched: {', '.join(mismatched)}") and ok
    else:
        record("routed as expected", True)
    return ok


def stage_api() -> bool:
    banner("6. API surface")
    result = subprocess.run([PY, str(ROOT / "scripts" / "test_api.py")],
                            capture_output=True, text=True)
    print("\n".join("      " + line for line in result.stdout.strip().splitlines()[-14:]))
    return record("test_api.py", result.returncode == 0,
                  "" if result.returncode == 0 else result.stderr.strip()[-300:])


def stage_tests() -> bool:
    banner("7. pytest")
    result = subprocess.run([PY, "-m", "pytest", "tests", "-q", "--no-header"],
                            capture_output=True, text=True, cwd=str(ROOT))
    lines = result.stdout.strip().splitlines()
    print("\n".join("      " + line for line in lines[-16:]))
    summary = next((line for line in reversed(lines) if "passed" in line or "failed" in line), "")
    return record("pytest", result.returncode == 0, summary.strip())


def stage_adaptation() -> bool:
    banner("8. remote-sensing adaptation")
    from backend.analysis import scene_labels

    described = scene_labels.describe()
    if described.get("available"):
        training = described.get("training") or {}
        metrics = training.get("metrics") or {}
        detail = (f"{described.get('model_id')} · {described.get('classes')} classes · "
                  f"held-out micro-F1 {metrics.get('micro_f1', '?')}")
        return record("BigEarthNet-MM head", True, detail)
    record("BigEarthNet-MM head", True,
           "not trained yet — the app runs on the measurement engine; "
           "see docs/TRAINING.md §A to train it")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="skip pytest and the API smoke")
    ap.add_argument("--demo-ready", action="store_true",
                    help="only what tomorrow's demo needs: data, controller, examples")
    ap.add_argument("--no-generate", action="store_true",
                    help="fail instead of generating missing demo data or examples")
    a = ap.parse_args()

    started = time.time()
    print("SatQuery AI — verification")
    print(f"repo: {ROOT}")
    print(f"python: {PY}")

    generate = not a.no_generate
    stage_imports()
    if not a.demo_ready:
        stage_compile()
    stage_demo_data(generate=generate)
    stage_controller()
    stage_examples(build=generate)
    if not a.quick and not a.demo_ready:
        stage_api()
        stage_tests()
    stage_adaptation()

    failed = [name for name, ok, _ in RESULTS if not ok]
    banner("summary")
    print(f"  {len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed "
          f"in {time.time() - started:.1f}s")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        return 1
    print("\n  Ready. Start the demo with:")
    print("    uvicorn backend.api.main:app --port 8000")
    print("    cd frontend && npm run dev")
    print("  Runbook: docs/DEMO_RUNBOOK.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
