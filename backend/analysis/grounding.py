"""Text-guided region grounding.

This is the second single-image task (CLAUDE.md section 4 recommends grounding
over captioning because it produces *visual evidence* rather than more prose).
A referring expression is resolved to a land-cover class plus optional spatial or
superlative qualifiers, and the matching connected regions become boxes in the
0..100 normalised coordinate space that VRSBench grounding uses.

When the phrase names something the class vocabulary does not cover, the module
says so and returns the closest available evidence flagged as unmatched, rather
than drawing a confident box around an arbitrary blob.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from backend.analysis.landcover import LandCoverResult, measure_land_cover
from backend.analysis.measurements import (BARE_SOIL, BUILT_UP, CLASS_LABELS, VEGETATION,
                                           WATER, GroundingMeasurement, Region)
from backend.analysis.regions import label_regions
from backend.config import settings
from backend.preprocessing.pipeline import PreparedImage

# Referring vocabulary -> canonical class. Longest phrases are matched first.
CLASS_PHRASES: Dict[str, Tuple[str, ...]] = {
    WATER: ("water body", "water bodies", "water-covered", "water covered", "waterbody",
            "water", "river", "lake", "pond", "reservoir", "canal", "channel", "creek",
            "stream", "sea", "coastline", "shoreline", "lagoon", "wetland", "flooded",
            "flood", "inundated", "tank"),
    VEGETATION: ("vegetation", "vegetated", "forest", "forested", "woodland", "trees",
                 "tree cover", "canopy", "crop", "crops", "cropland", "farmland",
                 "agricultural field", "agriculture", "plantation", "orchard", "grass",
                 "grassland", "park", "green area", "greenery", "paddy", "shrub"),
    BUILT_UP: ("built-up", "built up", "builtup", "building", "buildings", "urban",
               "settlement", "settlements", "residential", "industrial", "houses",
               "rooftops", "roof", "city", "town", "village", "infrastructure",
               "impervious", "road", "roads", "runway", "airport", "port", "bridge",
               "construction", "man-made", "manmade"),
    BARE_SOIL: ("bare soil", "bare ground", "bare land", "barren", "fallow", "soil",
                "sand", "sandy", "desert", "quarry", "exposed ground", "dry land"),
}

_DIRECTIONS = {
    "north": "north", "northern": "north", "top": "north", "upper": "north",
    "south": "south", "southern": "south", "bottom": "south", "lower": "south",
    "east": "east", "eastern": "east", "right": "east",
    "west": "west", "western": "west", "left": "west",
    "north-east": "north-east", "northeast": "north-east",
    "north-west": "north-west", "northwest": "north-west",
    "south-east": "south-east", "southeast": "south-east",
    "south-west": "south-west", "southwest": "south-west",
    "centre": "centre", "center": "centre", "central": "centre", "middle": "centre",
}
_SCREEN_EQUIVALENT = {"north": ("north", "top", "upper"), "south": ("south", "bottom", "lower"),
                      "east": ("east", "right"), "west": ("west", "left"),
                      "centre": ("centre",), "north-east": ("north-east", "upper-right"),
                      "north-west": ("north-west", "upper-left"),
                      "south-east": ("south-east", "lower-right"),
                      "south-west": ("south-west", "lower-left")}

_ALL_WORDS = ("all", "every", "each", "any ", "all the")
_LARGEST = ("largest", "biggest", "main", "major", "primary", "dominant", "widest")
_SMALLEST = ("smallest", "narrowest", "minor")


@dataclass
class ParsedPhrase:
    target_class: Optional[str]
    phrase: str
    direction: Optional[str] = None
    superlative: Optional[str] = None
    want_all: bool = False
    matched_term: Optional[str] = None


def parse_phrase(query: str) -> ParsedPhrase:
    """Resolve a referring expression to a class plus qualifiers."""
    q = " " + re.sub(r"\s+", " ", (query or "").lower()).strip() + " "
    target = None
    matched = None
    best_len = 0
    for cls, phrases in CLASS_PHRASES.items():
        for term in phrases:
            if term in q and len(term) > best_len:
                target, matched, best_len = cls, term, len(term)

    direction = None
    for word, canon in sorted(_DIRECTIONS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(word)}\b", q):
            direction = canon
            break

    superlative = None
    if any(w in q for w in _LARGEST):
        superlative = "largest"
    elif any(w in q for w in _SMALLEST):
        superlative = "smallest"

    return ParsedPhrase(target_class=target, phrase=(query or "").strip(),
                        direction=direction, superlative=superlative,
                        want_all=any(w in q for w in _ALL_WORDS), matched_term=matched)


def _matches_direction(region: Region, direction: str) -> bool:
    words = _SCREEN_EQUIVALENT.get(direction, (direction,))
    pos = (region.position or "").lower()
    return any(w in pos for w in words)


def ground_query(img: PreparedImage, query: str, land_cover: Optional[LandCoverResult] = None,
                 max_regions: int = 5, min_region_pixels: Optional[int] = None,
                 target_class: Optional[str] = None) -> Tuple[GroundingMeasurement, Dict[str, object]]:
    """Ground a referring expression in one image.

    ``target_class`` lets the controller pass an explicit class as a permitted
    task parameter, bypassing phrase parsing.
    """
    lc = land_cover or measure_land_cover(img, min_region_pixels=min_region_pixels)
    parsed = parse_phrase(query)
    if target_class:
        parsed.target_class = target_class
        parsed.matched_term = f"explicit parameter target_class={target_class}"

    limitations: List[str] = list(lc.measurement.quality.limitations)
    min_px = settings.min_region_pixels if min_region_pixels is None else int(min_region_pixels)

    if parsed.target_class is None:
        fallback = lc.measurement.dominant
        limitations.append(
            f"The phrase \"{parsed.phrase}\" does not name a land-cover class this system can "
            f"localise ({', '.join(CLASS_LABELS[c] for c in CLASS_PHRASES)}). "
            f"Returning the dominant class ({CLASS_LABELS.get(fallback, fallback)}) as the closest "
            "available evidence; treat the box as unverified.")
        mask = lc.mask(fallback)
        candidates = label_regions(mask, class_name=fallback, min_pixels=min_px,
                                   pixel_area_m2=img.pixel_area_m2,
                                   north_up=bool(img.info.crs), limit=max_regions,
                                   total_valid=int(img.valid.sum()),
                                   label=f"{CLASS_LABELS.get(fallback, fallback)} (phrase unmatched)")
        for r in candidates:
            r.score = round(min(r.score, 0.35), 3)
        selected = candidates[:1]
        measurement = GroundingMeasurement(
            method="phrase_unmatched_dominant_class", target_class=fallback,
            target_phrase=parsed.phrase, regions=selected,
            candidates_considered=len(candidates), land_cover=lc.measurement,
            quality=lc.measurement.quality.model_copy(update={"limitations": limitations}))
        return measurement, {"mask": mask, "parsed": parsed}

    mask = lc.mask(parsed.target_class)
    candidates = label_regions(
        mask, class_name=parsed.target_class, min_pixels=min_px,
        pixel_area_m2=img.pixel_area_m2, north_up=bool(img.info.crs),
        limit=max(max_regions, 8), total_valid=int(img.valid.sum()))

    selected = list(candidates)
    qualifier_parts: List[str] = []
    if parsed.direction:
        filtered = [r for r in selected if _matches_direction(r, parsed.direction)]
        if filtered:
            selected = filtered
            qualifier_parts.append(parsed.direction)
        else:
            limitations.append(
                f"No {CLASS_LABELS.get(parsed.target_class, parsed.target_class)} region falls in the "
                f"{parsed.direction} of the scene; the spatial qualifier was dropped.")
    if parsed.superlative == "smallest":
        selected = sorted(selected, key=lambda r: r.area_px)[:1]
        qualifier_parts.append("smallest")
    elif parsed.superlative == "largest" or not parsed.want_all:
        selected = sorted(selected, key=lambda r: -r.area_px)[:1]
        if parsed.superlative:
            qualifier_parts.append("largest")
    else:
        selected = sorted(selected, key=lambda r: -r.area_px)[:max_regions]
        qualifier_parts.append("all instances")

    if not candidates:
        limitations.append(
            f"No {CLASS_LABELS.get(parsed.target_class, parsed.target_class)} region above "
            f"{min_px} pixels was found in this image, so no box is returned.")

    cls_stat = lc.measurement.get(parsed.target_class)
    if cls_stat and cls_stat.reliability in ("low", "unavailable"):
        limitations.append(
            f"{CLASS_LABELS.get(parsed.target_class, parsed.target_class)} detection reliability for "
            f"this product is '{cls_stat.reliability}': {cls_stat.evidence_basis}.")

    measurement = GroundingMeasurement(
        method="class_mask_connected_components_v1",
        target_class=parsed.target_class, target_phrase=parsed.phrase,
        qualifier=", ".join(qualifier_parts) or None,
        regions=selected, candidates_considered=len(candidates),
        land_cover=lc.measurement,
        quality=lc.measurement.quality.model_copy(update={"limitations": limitations}))
    return measurement, {"mask": mask, "parsed": parsed}
