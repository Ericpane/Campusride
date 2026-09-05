"""
Django system checks for the ML model layer.

These run automatically before `runserver`, `migrate`, and `test` (and can be
run explicitly with `python manage.py check`), so a broken or mismatched
model file is caught immediately at startup/CI instead of silently degrading
every request to the heuristic fallback in ml_service.py — which is exactly
what happened with the two bugs this project shipped with (joblib-saved
models loaded with plain pickle.load, and predict_demand sending 5 features
to a model trained on 2).
"""
from __future__ import annotations

from django.core.checks import Error, register

# Feature vectors ml_service.py actually builds and sends to each model.
# Keep these in sync with predict_p_board() / predict_demand() in
# ml_service.py — that's the whole point of this check.
_EXPECTED_FEATURES = {
    "no_show_model.pkl": 5,   # waiting_minutes, detour_km, hour_of_day, day_of_week, dist_to_rider_km
    "demand_model.pkl": 2,    # hour_of_day, day_of_week
}


@register()
def check_ml_models(app_configs, **kwargs):
    """
    Fails loudly (Error, not Warning) if either model file:
      - fails to load via ml_service._load_pickle, or
      - loads but its n_features_in_ doesn't match the feature vector
        ml_service.py actually sends it.

    A model that fails to load silently falls back to the heuristic in
    ml_service.py, which is a legitimate degraded mode for local dev without
    the .pkl files present — but it must never happen unnoticed in an
    environment where the files are supposed to be there. This check makes
    that failure visible instead of silent.
    """
    errors = []
    from . import ml_service

    for filename, expected_n in _EXPECTED_FEATURES.items():
        path = ml_service._DIR / filename
        if not path.exists():
            # Missing file is a legitimate "run without ML" dev mode --
            # ml_service.py already handles this via its heuristic fallback.
            # Don't error; just don't check further for this file.
            continue

        ml_service._load_pickle.cache_clear()
        model = ml_service._load_pickle(filename)
        if model is None:
            errors.append(Error(
                f"{filename} exists but failed to load (returned None). "
                "predict_p_board()/predict_demand() will silently fall back "
                "to the heuristic for every request.",
                id="rides.E001",
            ))
            continue

        n_features = getattr(model, "n_features_in_", None)
        if n_features is None:
            # Not every estimator exposes this; nothing further to check.
            continue
        if n_features != expected_n:
            errors.append(Error(
                f"{filename} expects {n_features} feature(s) but "
                f"ml_service.py sends {expected_n} feature(s). Every "
                "prediction call will raise ValueError internally and "
                "silently fall back to the heuristic.",
                id="rides.E002",
            ))

    return errors
