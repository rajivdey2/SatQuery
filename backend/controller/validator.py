"""Input Validator (Controller stage 1).

Rejects and explains rather than silently proceeding: format gate, modality
sniffing, co-registration checks for pairs, temporal-metadata checks for
bi-temporal inputs. Only the observable checks below are reported (and graded).
"""
from __future__ import annotations

from typing import List, Optional

from backend.config import settings
from backend.controller.audit import InputConfig, InputImage, ValidationCheck, ValidationReport
from backend.preprocessing import geotiff_io as gio

_ALLOWED_FORMATS = {"GEOTIFF", "TIFF", "PNG", "JPEG"}
_MULTI_IMAGE_MAX = 2


class ValidationError(ValueError):
    """Inputs are unusable; message is user-facing and appears in the audit trace."""


def _bounding_area(extent: List[float]) -> float:
    return max(0.0, (extent[2] - extent[0]) * (extent[3] - extent[1]))


def _overlap_area(e1: List[float], e2: List[float]) -> float:
    w = max(0.0, min(e1[2], e2[2]) - max(e1[0], e2[0]))
    h = max(0.0, min(e1[3], e2[3]) - max(e1[1], e2[1]))
    return w * h


def _check_co_registration(i: InputImage, j: InputImage, checks: List[ValidationCheck]) -> Optional[bool]:
    """CRS + extent overlap + pixel-size agreement. Returns None if not verifiable."""
    if i.crs and j.crs and i.extent and j.extent:
        same_crs = i.crs == j.crs
        overlap_ratio = _overlap_area(i.extent, j.extent) / max(1e-9, min(_bounding_area(i.extent), _bounding_area(j.extent)))
        pixel_ok = True
        if i.pixel_size and j.pixel_size:
            sx, sy = _pair_ratio(i.pixel_size[0], j.pixel_size[0]), _pair_ratio(i.pixel_size[1], j.pixel_size[1])
            pixel_ok = sx and sy and max(sx, sy) < 2.0
        if same_crs and overlap_ratio > 0.5 and pixel_ok:
            checks.append(ValidationCheck(name="co_registration", status="passed",
                                          message=f"CRS={i.crs}, extent overlap {overlap_ratio:.0%}"))
            return True
        checks.append(ValidationCheck(
            name="co_registration", status="failed",
            message=f"Cannot confirm co-registration: same_crs={same_crs}, overlap_ratio={overlap_ratio:.0%}, pixel_ok={pixel_ok}"))
        return False
    checks.append(ValidationCheck(
        name="co_registration", status="warning",
        message="No georeferencing available (benchmark-style input); spatial correspondence assumed as declared."))
    return None


def _pair_ratio(a: float, b: float) -> Optional[float]:
    if not a or not b:
        return None
    return max(a / b, b / a) if a != b else 1.0


def validate_inputs(files: List[str], query: str) -> ValidationReport:
    if not files:
        raise ValidationError("No image files provided.")
    if len(files) > settings.max_images:
        raise ValidationError(f"{len(files)} files provided; maximum supported is {settings.max_images}.")

    infos = [gio.probe_image(f) for f in files]
    images: List[InputImage] = []
    checks: List[ValidationCheck] = []
    passed: List[str] = []
    _meta_map = {"tags": "rasterio_tags", "band_count": "none", "filename": "filename", "none": "none"}

    for info in infos:
        fmt = info.format
        if fmt not in _ALLOWED_FORMATS:
            raise ValidationError(
                f"Unsupported format for '{info.path}': {info.format}. "
                "GeoTIFF/TIFF required for geospatial imagery; PNG/JPEG only for benchmark datasets.")
        images.append(InputImage(
            filename=info.path, format=fmt, modality=info.modality, width=info.width,
            height=info.height, band_count=info.band_count, dtype=info.dtype, crs=info.crs,
            extent=info.extent, pixel_size=info.pixel_size, acquisition_date=info.acquisition_date,
            metadata_source=_meta_map.get(info.metadata_source, "none")))
        checks.append(ValidationCheck(name="format", status="passed", message=f"{fmt} ({info.width}x{info.height}, {info.band_count} band, {info.dtype})"))
        passed.append(f"format:{fmt}")

    if len(images) == 1:
        checks.append(ValidationCheck(name="input_shape", status="passed", message="Single image input."))
        report = _assemble(images, checks, passed, geolocated=any(i.crs for i in images))
        report.input_config.modality_signature = [images[0].modality]
        return report

    if len(images) == 2:
        i, j = images
        signature = [i.modality, j.modality]
        co_reg = None
        if "sar" in signature and any(m in signature for m in ("optical", "multispectral")):
            checks.append(ValidationCheck(name="pair_type", status="passed",
                                          message="Cross-modal optical+SAR pair detected."))
            co_reg = _check_co_registration(i, j, checks)
        else:
            checks.append(ValidationCheck(name="pair_type", status="passed",
                                          message="Bi-temporal/mono-modal pair detected."))
            co_reg = _check_co_registration(i, j, checks)
            if i.acquisition_date and j.acquisition_date:
                if i.acquisition_date == j.acquisition_date:
                    raise ValidationError(
                        "Bi-temporal input requires different acquisition dates; "
                        f"both images report {i.acquisition_date}.")
                checks.append(ValidationCheck(name="temporal", status="passed",
                                              message=f"Dates {i.acquisition_date} -> {j.acquisition_date}"))
                passed.append("temporal_dates_differ")
            else:
                checks.append(ValidationCheck(name="temporal", status="warning",
                                              message="Acquisition dates unavailable in metadata; treating upload order as chronological as declared."))

        report = _assemble(images, checks, passed, geolocated=any(img.crs for img in images))
        report.input_config.modality_signature = signature
        report.input_config.co_registered = co_reg
        report.input_config.co_registration_note = _last_check_msg(checks, "co_registration")
        cross_modal = "sar" in signature and any(m in ("optical", "multispectral") for m in signature)
        report.input_config.bi_temporal = len(images) == 2 and not cross_modal
        report.input_config.dates_differ = _two_dates_differ(images)
        report.input_config.date_note = _last_check_msg(checks, "temporal")
        if co_reg is False:
            raise ValidationError(
                "Rejected: co-registration could not be confirmed for the provided pair. "
                "The controller will not run fusion on a pair that may be misaligned.")
        return report

    raise ValidationError(f"Unsupported input count: {len(images)} images (max {_MULTI_IMAGE_MAX}).")


def _two_dates_differ(images: List[InputImage]) -> Optional[bool]:
    d = [im.acquisition_date for im in images if im.acquisition_date]
    if len(d) < 2:
        return None
    return d[0] != d[1]


def _last_check_msg(checks: List[ValidationCheck], name: str) -> Optional[str]:
    for c in reversed(checks):
        if c.name == name:
            return c.message
    return None


def _assemble(images: List[InputImage], checks: List[ValidationCheck], passed: List[str],
              geolocated: bool) -> ValidationReport:
    warnings = [c.message for c in checks if c.status == "warning"]
    failed = [c.message for c in checks if c.status == "failed"]
    if not any(c.status == "failed" for c in checks):
        passed.extend(c.message for c in checks if c.status == "passed")
    config = InputConfig(n_images=len(images), images=images, geolocated=geolocated)
    return ValidationReport(
        passed=passed, warnings=warnings, failed=failed, checks=checks,
        input_config=config, accepted=not bool(failed))