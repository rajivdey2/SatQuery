"""Measurement engine for SatQuery AI.

Modules here turn pixels into numbers: spectral and radar indices
(``indices``), thresholds with a recorded quality (``thresholds``), land-cover
classification (``landcover``), connected-region geometry (``regions``),
bi-temporal change (``change``), optical+SAR joint extraction (``fusion``),
referring-expression grounding (``grounding``), the adapted BigEarthNet-MM scene
head (``scene_labels``), rendered visual evidence (``render``) and the narration
layer that may only restate measured values (``narrate``).

The specialists in ``backend/specialists`` are thin wrappers over these; the
controller never calls this package directly.
"""
