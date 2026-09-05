"""
ML layer: demand + no-show models with safe fallbacks.
Models are optional; app runs without sklearn/pickles.
"""
from __future__ import annotations

import logging
import math
import pickle
from functools import lru_cache
from pathlib import Path

try:
    import joblib
except ImportError:  # pragma: no cover - joblib ships with scikit-learn
    joblib = None

logger = logging.getLogger(__name__)
_DIR = Path(__file__).resolve().parent

_DEFAULT_CAL = {
    "distance_mean": 11.55,
    "distance_std": 9.15,
    "fare_per_km": 133.33,
    "peak_prob": 0.46,
}


@lru_cache(maxsize=1)
def get_calibration():
    data = _load_pickle("calibration_stats.pkl")
    if isinstance(data, dict):
        out = dict(_DEFAULT_CAL)
        out.update({k: float(v) for k, v in data.items() if v is not None})
        return out
    return dict(_DEFAULT_CAL)


@lru_cache(maxsize=1)
def _load_pickle(name: str):
    """
    Load a model file that may have been saved with either `pickle.dump`
    (e.g. calibration_stats.pkl, a plain dict) or `joblib.dump` (the
    sklearn Pipeline/estimator model files). joblib wraps numpy arrays in
    its own NumpyArrayWrapper class, which plain `pickle.load` cannot
    reconstruct (fails with "STACK_GLOBAL requires str" or similar) --
    so we try joblib first for anything that looks like a model, falling
    back to plain pickle for simple data files.
    """
    path = _DIR / name
    if joblib is not None:
        try:
            return joblib.load(path)
        except Exception as e:
            logger.debug("joblib.load(%s) failed, trying plain pickle: %s", name, e)
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception as e:
        logger.warning("Could not load %s: %s", name, e)
        return None


def predict_p_board(
    waiting_minutes: float = 0.0,
    detour_km: float = 0.0,
    hour_of_day: int = 12,
    day_of_week: int = 0,
    dist_to_rider_km: float = 0.5,
) -> float:
    """
    P(board | features). Uses logistic pipeline if available, else heuristic.
    """
    model = _load_pickle("no_show_model.pkl")
    # Training label was often `boarded`; treat predict_proba[:,1] as P(board)
    features = [[
        float(waiting_minutes),
        float(detour_km),
        float(hour_of_day),
        float(day_of_week),
        float(dist_to_rider_km),
    ]]
    if model is not None:
        try:
            if hasattr(model, "predict_proba"):
                proba = model.predict_proba(features)[0]
                # binary: class 1 = boarded
                p = float(proba[1]) if len(proba) > 1 else float(proba[0])
                return max(0.05, min(0.99, p))
            if hasattr(model, "predict"):
                y = float(model.predict(features)[0])
                return max(0.05, min(0.99, y if 0 <= y <= 1 else 0.7))
        except Exception as e:
            logger.warning("no_show predict failed: %s", e)

    # Heuristic fallback: longer wait / large detour → lower show-up
    p = 0.85 - 0.02 * waiting_minutes - 0.15 * detour_km - 0.05 * dist_to_rider_km
    return max(0.2, min(0.95, p))


def predict_demand(hour_of_day: int = 12, day_of_week: int = 0, base_context: float = 1.0) -> float:
    """Relative demand score (higher = busier)."""
    model = _load_pickle("demand_model.pkl")
    # demand_model.pkl was trained on just (hour_of_day, day_of_week) --
    # it has n_features_in_ == 2. Passing 5 features raised a ValueError on
    # every call, which the except below silently swallowed, so this model
    # was never actually used.
    features = [[float(hour_of_day), float(day_of_week)]]
    if model is not None:
        try:
            y = float(model.predict(features)[0])
            return max(0.1, y)
        except Exception as e:
            logger.warning("demand predict failed: %s", e)

    cal = get_calibration()
    peak = float(cal.get("peak_prob", 0.46))
    # Simple campus-like peaks: morning + late afternoon
    h = hour_of_day % 24
    if 7 <= h <= 9 or 16 <= h <= 18:
        return 3.0 + peak
    if 12 <= h <= 14:
        return 2.0
    return 1.0 + 0.5 * peak


def choose_anchor_location(campus_locations, hour=None, day=None, driver_lat=None, driver_lng=None):
    """
    Pick a system anchor CampusLocation using demand heuristic.
    Prefer central / first locations weighted by demand score.

    If driver coordinates are given, locations that sit right on top of the
    driver's own position are excluded when a farther alternative exists.
    A same-spot anchor makes the driver->anchor direction vector near-zero
    length, which makes the downstream alignment check (which compares
    driver->anchor against pickup->dropoff) numerically meaningless and
    causes otherwise-normal riders to be rejected.
    """
    from django.utils import timezone
    from .geofence import haversine_distance
    from .constants import MIN_ANCHOR_DISTANCE_METERS

    now = timezone.now()
    hour = hour if hour is not None else now.hour
    day = day if day is not None else now.weekday()
    locs = list(campus_locations)
    if not locs:
        return None

    if driver_lat is not None and driver_lng is not None:
        far_enough = [
            loc for loc in locs
            if haversine_distance(driver_lat, driver_lng, loc.latitude, loc.longitude)
            >= MIN_ANCHOR_DISTANCE_METERS
        ]
        if far_enough:
            locs = far_enough
        # If every campus location is close to the driver (tiny campus / edge
        # case), fall through and keep the full list rather than returning
        # nothing — the matching layer's own fallback still guards alignment.

    demand = predict_demand(hour, day)
    # Stable choice: pick location index biased by demand, not random chaos
    idx = int(math.floor(demand * 10)) % len(locs)
    return locs[idx]
