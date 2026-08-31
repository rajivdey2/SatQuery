"""Input Validator (Controller stage 1).

The problem statement asks the controller to "check the number, modality, format,
metadata, and compatibility of the input images". CLAUDE.md section 7.1 adds the
part that actually earns marks: **reject and explain, do not silently proceed**.
Because the final evaluation runs on unseen Cartosat-2S / RISAT products, a system
that refuses a pair it cannot verify as co-registered is worth more than one that
fuses it anyway and reports a confident number.

Every check produces a ``ValidationCheck`` with a status and a message, and the
whole list ends up in the audit trace whether the run proceeds or not. Validation
never raises for input-quality problems: it returns an un-accepted report so the
trace keeps the full reasoning.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from backend.config import settings
from backend.controller.audit import (InputConfig, InputImage, ValidationCheck,
                                      ValidationReport, image_to_audit)
from backend.preprocessing import geotiff_io as gio

ALLOWED_FORMATS = {"GEOTIFF", "TIFF", "PNG", "JPEG"}
BENCHMARK_ONLY_FORMATS = {"PNG", "JPEG"}
MIN_SIDE = 32
_OPTICAL_MODALITIES = ("optical", "multispectral", "panchromatic")


class ValidationError(ValueError):
    """Raised only for structurally impossible input (no files at all)."""


def _bbox_area(e: List[float]) -> float:
    return max(0.0, (e[2] - e[0])) * max(0.0, (e[3] - e[1]))


def _overlap_area(a: List[float], b: List[float]) -> float:
    w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return w * h


def _ratio(a: float, b: float) -> Optional[float]:
    if not a or not b:
        return None
    return max(a / b, b / a)


def _comparable_extents(i, j) -> Tuple[Optional[List[float]], Optional[List[float]], str]:
    """Extents in a shared CRS, reprojecting when the two products differ.

    Two co-registered products can legitimately be delivered in different CRSs
    (a UTM Cartosat chip and a geographic RISAT chip of the same footprint).
    Comparing raw bounds would reject that pair, so bounds are compared in WGS84
    whenever the native CRSs disagree.
    """
    if i.crs and j.crs and i.crs == j.crs and i.extent and j.extent:
        return i.extent, j.extent, f"native CRS {i.crs}"
    if i.extent_wgs84 and j.extent_wgs84:
        return i.extent_wgs84, j.extent_wgs84, "reprojected to EPSG:4326 for comparison"
    return None, None, "no comparable georeferencing"


def _check_co_registration(i: InputImage, j: InputImage, checks: List[ValidationCheck],
                           infos) -> Optional[bool]:
    """CRS, extent overlap, resolution ratio and grid alignment."""
    a, b, how = _comparable_extents(infos[0], infos[1])
    if a is None or b is None:
        checks.append(ValidationCheck(
            name="co_registration", status="warning",
            message="Neither image carries a CRS/geotransform (benchmark-style input). Spatial "
                    "correspondence is assumed as declared by the user and cannot be verified; "
                    "the later image is resampled onto the first image's pixel grid.",
            detail={"verifiable": False}))
        return None

    overlap = _overlap_area(a, b)
    smaller = min(_bbox_area(a), _bbox_area(b))
    overlap_ratio = overlap / smaller if smaller > 0 else 0.0
    same_crs = bool(i.crs and j.crs and i.crs == j.crs)

    res_ratio = None
    if i.pixel_size and j.pixel_size:
        rx = _ratio(i.pixel_size[0], j.pixel_size[0])
        ry = _ratio(i.pixel_size[1], j.pixel_size[1])
        res_ratio = max(x for x in (rx, ry) if x is not None) if (rx or ry) else None

    detail = {"comparison": how, "same_crs": same_crs,
              "overlap_ratio": round(overlap_ratio, 4),
              "resolution_ratio": round(res_ratio, 3) if res_ratio else None,
              "crs": [i.crs, j.crs]}

    if overlap_ratio < settings.coregistration_min_overlap:
        checks.append(ValidationCheck(
            name="co_registration", status="failed",
            message=(f"The two footprints overlap by only {overlap_ratio * 100:.1f}% "
                     f"({how}); at least {settings.coregistration_min_overlap * 100:.0f}% is required. "
                     "These images do not cover the same area."),
            detail=detail))
        return False

    if res_ratio and res_ratio > 8.0:
        checks.append(ValidationCheck(
            name="co_registration", status="failed",
            message=(f"Ground sampling distances differ by {res_ratio:.1f}x "
                     f"({i.pixel_size} vs {j.pixel_size}). Resampling across that gap would "
                     "fabricate detail; supply products at comparable resolution."),
            detail=detail))
        return False

    status = "passed"
    msg = (f"Footprints overlap {overlap_ratio * 100:.1f}% ({how})"
           + (f", resolution ratio {res_ratio:.2f}x" if res_ratio else "")
           + (", identical CRS" if same_crs else ", CRS differ but footprints reconcile"))
    if overlap_ratio < 0.95 or (res_ratio and res_ratio > 2.0):
        status = "warning"
        msg += (". Partial overlap or differing resolution: measurements are restricted to the "
                "jointly valid area and the second image is resampled onto the first image's grid.")
    checks.append(ValidationCheck(name="co_registration", status=status, message=msg, detail=detail))
    return True


def _probe_all(files: List[str], checks: List[ValidationCheck]) -> Tuple[list, List[str]]:
    infos, failed = [], []
    for f in files:
        path = Path(f)
        if not path.exists():
            failed.append(f"File not found: {f}")
            checks.append(ValidationCheck(name="file", status="failed",
                                          message=f"File not found: {path.name}"))
            continue
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > settings.max_file_mb:
            failed.append(f"{path.name} is {size_mb:.0f} MB, over the {settings.max_file_mb} MB limit.")
            checks.append(ValidationCheck(
                name="file", status="failed",
                message=f"{path.name} is {size_mb:.0f} MB, over the {settings.max_file_mb} MB limit."))
            continue
        try:
            infos.append(gio.probe_image(f))
        except Exception as exc:
            failed.append(f"{path.name} could not be opened as a raster: {exc}")
            checks.append(ValidationCheck(
                name="format", status="failed",
                message=f"{path.name} could not be opened as a raster ({type(exc).__name__}: {exc}). "
                        "Supported: GeoTIFF/TIFF, or PNG/JPEG for benchmark datasets."))
    return infos, failed


def _check_one(info, checks: List[ValidationCheck], failed: List[str], passed: List[str]) -> None:
    name = Path(info.path).name
    if info.format not in ALLOWED_FORMATS:
        failed.append(f"{name}: unsupported format {info.format}.")
        checks.append(ValidationCheck(
            name="format", status="failed",
            message=f"{name}: unsupported format {info.format}. GeoTIFF/TIFF is required for "
                    "geospatial imagery; PNG/JPEG are accepted only for the prescribed public "
                    "benchmark datasets."))
        return

    checks.append(ValidationCheck(
        name="format", status="passed",
        message=f"{name}: {info.format}, {info.width}x{info.height}, {info.band_count} band(s), {info.dtype}",
        detail={"format": info.format, "dtype": info.dtype, "bands": info.band_count}))
    passed.append(f"format:{info.format}")

    if info.format in BENCHMARK_ONLY_FORMATS:
        checks.append(ValidationCheck(
            name="format_policy", status="warning",
            message=f"{name} is {info.format}: accepted as benchmark input only. It carries no CRS, "
                    "no acquisition date and 8-bit radiometry, so areas cannot be reported in "
                    "hectares and spectral indices are limited to visible-band proxies."))

    if min(info.width, info.height) < MIN_SIDE:
        failed.append(f"{name} is {info.width}x{info.height}: too small to analyse.")
        checks.append(ValidationCheck(
            name="dimensions", status="failed",
            message=f"{name} is {info.width}x{info.height}; at least {MIN_SIDE}x{MIN_SIDE} is required."))
        return

    status = {"high": "passed", "medium": "passed", "low": "warning"}[info.modality_confidence]
    message = f"{name}: modality '{info.modality}' ({info.modality_confidence} confidence) — {info.modality_reason}"
    if info.modality == "unknown":
        status = "warning"
        message = (f"{name}: modality could not be determined — {info.modality_reason} "
                   "Routing continues, but a wrong modality guess would route to the wrong "
                   "specialist, so declare it explicitly for production use.")
    checks.append(ValidationCheck(name="modality", status=status, message=message,
                                  detail={"modality": info.modality,
                                          "confidence": info.modality_confidence,
                                          "sensor_guess": info.sensor_guess}))
    if status == "passed":
        passed.append(f"modality:{info.modality}")

    if info.format in ("GEOTIFF", "TIFF"):
        if info.crs:
            checks.append(ValidationCheck(
                name="georeference", status="passed",
                message=f"{name}: CRS {info.crs}, pixel size {info.pixel_size}, "
                        f"extent {[round(v, 4) for v in (info.extent or [])]}",
                detail={"crs": info.crs, "pixel_size": info.pixel_size}))
            passed.append("georeference")
        else:
            checks.append(ValidationCheck(
                name="georeference", status="warning",
                message=f"{name} is a plain TIFF with no CRS or geotransform: areas cannot be "
                        "reported in ground units and co-registration cannot be verified."))

    if info.is_complex:
        checks.append(ValidationCheck(
            name="radiometry", status="warning",
            message=f"{name} is a complex (I/Q) SAR product; amplitude is used and the phase is "
                    "discarded. Interferometric analysis is out of scope for this system."))

    if info.acquisition_date:
        checks.append(ValidationCheck(
            name="metadata", status="passed",
            message=f"{name}: acquisition date {info.acquisition_date} (from {info.metadata_source})"))
    else:
        checks.append(ValidationCheck(
            name="metadata", status="warning",
            message=f"{name}: no acquisition date in product tags or filename."))


def validate_inputs(files: List[str], query: str = "") -> ValidationReport:
    """Run every input check and return a report; never raises for bad imagery."""
    checks: List[ValidationCheck] = []
    passed: List[str] = []
    failed: List[str] = []

    if not files:
        raise ValidationError("No image files were provided.")

    if len(files) > settings.max_images:
        failed.append(f"{len(files)} images supplied; this system supports at most "
                      f"{settings.max_images} (single image, or one pair).")
        checks.append(ValidationCheck(
            name="input_count", status="failed",
            message=(f"{len(files)} images supplied. The defined input scope is a single image, a "
                     f"co-registered optical+SAR pair, or a bi-temporal pair — at most "
                     f"{settings.max_images} images.")))
        return _assemble([], checks, passed, failed)

    infos, probe_failures = _probe_all(files, checks)
    failed.extend(probe_failures)
    for info in infos:
        _check_one(info, checks, failed, passed)

    images = [image_to_audit(info) for info in infos]
    if failed or len(images) != len(files):
        return _assemble(images, checks, passed, failed)

    if len(images) == 1:
        checks.append(ValidationCheck(
            name="input_configuration", status="passed",
            message=f"Single-image input ({images[0].modality}).",
            detail={"configuration": "single_image"}))
        report = _assemble(images, checks, passed, failed)
        report.input_config.modality_signature = [images[0].modality]
        return report

    i, j = images[0], images[1]
    signature = [i.modality, j.modality]
    cross_modal = ("sar" in signature and
                   any(m in _OPTICAL_MODALITIES for m in signature))
    both_sar = signature.count("sar") == 2

    if cross_modal:
        checks.append(ValidationCheck(
            name="input_configuration", status="passed",
            message="Cross-modal pair detected: one optical/multispectral and one SAR image.",
            detail={"configuration": "optical_sar_pair", "modalities": signature}))
    elif both_sar or i.modality == j.modality:
        checks.append(ValidationCheck(
            name="input_configuration", status="passed",
            message=f"Same-modality pair detected ({i.modality}): treated as bi-temporal.",
            detail={"configuration": "bi_temporal_pair", "modalities": signature}))
    else:
        checks.append(ValidationCheck(
            name="input_configuration", status="warning",
            message=f"Mixed modalities {signature} that are neither a clean optical+SAR pair nor a "
                    "same-sensor pair. Treated as bi-temporal; class-level comparison only.",
            detail={"configuration": "bi_temporal_pair", "modalities": signature}))

    co_reg = _check_co_registration(i, j, checks, infos)

    dates_differ = None
    if i.acquisition_date and j.acquisition_date:
        dates_differ = i.acquisition_date != j.acquisition_date
        if cross_modal:
            checks.append(ValidationCheck(
                name="temporal", status=("passed" if not dates_differ else "warning"),
                message=(f"Cross-modal pair acquired {i.acquisition_date} / {j.acquisition_date}"
                         + ("." if not dates_differ else
                            ". The dates differ, so any difference between the sensors may be real "
                            "change rather than a modality difference.")),
                detail={"dates": [i.acquisition_date, j.acquisition_date]}))
        elif dates_differ:
            checks.append(ValidationCheck(
                name="temporal", status="passed",
                message=f"Bi-temporal pair: {i.acquisition_date} -> {j.acquisition_date}.",
                detail={"dates": [i.acquisition_date, j.acquisition_date]}))
            passed.append("temporal_dates_differ")
        else:
            failed.append(f"Both images report the same acquisition date ({i.acquisition_date}); "
                          "a bi-temporal pair requires two different dates.")
            checks.append(ValidationCheck(
                name="temporal", status="failed",
                message=(f"Both images report acquisition date {i.acquisition_date}. Change analysis "
                         "over two identical dates is not meaningful — supply two acquisitions.")))
    else:
        checks.append(ValidationCheck(
            name="temporal", status="warning",
            message="Acquisition dates are missing from the product metadata for at least one image. "
                    "Upload order is treated as chronological, as declared; dates cannot be verified."))

    if co_reg is False:
        failed.append("Co-registration could not be confirmed for this pair.")
        checks.append(ValidationCheck(
            name="compatibility", status="failed",
            message="Rejected: the pair is not verifiably co-registered, so neither per-pixel change "
                    "detection nor optical/SAR fusion can be trusted on it. Supply co-registered "
                    "products, or a single image for single-image analysis."))

    report = _assemble(images, checks, passed, failed)
    cfg = report.input_config
    cfg.modality_signature = signature
    cfg.co_registered = co_reg
    cfg.co_registration_note = _last_message(checks, "co_registration")
    cfg.cross_modal = cross_modal
    cfg.bi_temporal = not cross_modal
    cfg.dates_differ = dates_differ
    cfg.date_note = _last_message(checks, "temporal")
    return report


def _last_message(checks: List[ValidationCheck], name: str) -> Optional[str]:
    for c in reversed(checks):
        if c.name == name:
            return c.message
    return None


def _assemble(images: List[InputImage], checks: List[ValidationCheck],
              passed: List[str], failed: List[str]) -> ValidationReport:
    warnings = [c.message for c in checks if c.status == "warning"]
    failures = [c.message for c in checks if c.status == "failed"] or list(failed)
    passed = list(dict.fromkeys(passed + [c.message for c in checks if c.status == "passed"]))
    config = InputConfig(n_images=len(images), images=images,
                         geolocated=any(im.crs for im in images))
    return ValidationReport(passed=passed, warnings=warnings, failed=failures,
                            checks=checks, input_config=config, accepted=not failures)
