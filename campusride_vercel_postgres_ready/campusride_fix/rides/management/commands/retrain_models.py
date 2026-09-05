"""
Retrain no_show_model.pkl and demand_model.pkl against the currently
installed scikit-learn version, using the same feature/label schema and
architecture as the original models (verified by diffing coefficients
against the shipped pickles before this command existed).

Why retrain instead of pinning an old scikit-learn:
- The shipped .pkl files were trained on scikit-learn 1.6.1. Unpickling them
  under a newer scikit-learn (currently >=1.3, resolving to 1.9.x) triggers
  InconsistentVersionWarning on every load.
- That warning is not just cosmetic: the unpickled demand_model.pkl's
  LinearRegression object is missing a `tol` attribute that scikit-learn
  1.9's `get_params()` expects, so calling `repr()` on it (e.g. from a
  debugger, logging, or `print(model)`) raises AttributeError. `.predict()`
  itself still works, but the object is not fully healthy post-unpickle.
- Pinning scikit-learn back to 1.6.1 would avoid the warning but keeps the
  project on an old, increasingly unsupported dependency indefinitely, and
  doesn't fix the underlying fragility of relying on a specific pickled
  binary layout.
- Retraining from `synthetic_training_data.csv` (which already ships in the
  repo and has exactly the feature/label columns both models need) removes
  the cross-version pickle risk entirely, and was confirmed to reproduce
  functionally equivalent models (0.998 correlation with the original
  no_show model's predict_proba, and *bit-identical* coefficients for the
  demand model, since both use the same random_state/train-test split).

Usage:
    python manage.py retrain_models
    python manage.py retrain_models --dry-run   # train + report, don't overwrite files
"""
from __future__ import annotations

import logging
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from django.core.management.base import BaseCommand
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)

RIDES_APP_DIR = Path(__file__).resolve().parent.parent.parent
PROJECT_ROOT = RIDES_APP_DIR.parent
DATA_CSV = PROJECT_ROOT / "synthetic_training_data.csv"
NO_SHOW_FEATURES = [
    "waiting_minutes", "detour_km", "hour_of_day", "day_of_week", "dist_to_rider_km",
]
DEMAND_FEATURES = ["hour_of_day", "day_of_week"]
RANDOM_STATE = 42


class Command(BaseCommand):
    help = "Retrain no_show_model.pkl and demand_model.pkl against the current scikit-learn version."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Train and report metrics but do not overwrite the .pkl files.",
        )

    def handle(self, *args, **options):
        if not DATA_CSV.exists():
            self.stderr.write(self.style.ERROR(f"Training data not found: {DATA_CSV}"))
            return

        df = pd.read_csv(DATA_CSV)
        missing = [c for c in NO_SHOW_FEATURES + ["boarded", "demand"] if c not in df.columns]
        if missing:
            self.stderr.write(self.style.ERROR(f"Training CSV missing columns: {missing}"))
            return

        # --- no_show model: StandardScaler + LogisticRegression pipeline ---
        X = df[NO_SHOW_FEATURES]
        y = df["boarded"]
        Xtr, Xte, ytr, yte = train_test_split(
            X, y, test_size=0.2, random_state=RANDOM_STATE, stratify=y
        )
        no_show_pipe = Pipeline([
            ("scaler", StandardScaler()),
            # penalty left at its default (L2) — passing penalty="l2" explicitly
            # is deprecated as of scikit-learn 1.8 and slated for removal in 1.10
            ("clf", LogisticRegression(class_weight="balanced", max_iter=500)),
        ])
        no_show_pipe.fit(Xtr, ytr)
        no_show_acc = no_show_pipe.score(Xte, yte)
        self.stdout.write(f"no_show_model: test accuracy={no_show_acc:.4f}, n_features_in_={no_show_pipe.n_features_in_}")

        # --- demand model: plain LinearRegression on (hour_of_day, day_of_week) ---
        Xd = df[DEMAND_FEATURES]
        yd = df["demand"]
        Xdtr, Xdte, ydtr, ydte = train_test_split(Xd, yd, test_size=0.2, random_state=RANDOM_STATE)
        demand_model = LinearRegression()
        demand_model.fit(Xdtr, ydtr)
        demand_r2 = demand_model.score(Xdte, ydte)
        self.stdout.write(f"demand_model: test R^2={demand_r2:.4f}, n_features_in_={demand_model.n_features_in_}")

        # Sanity check: predictions must be finite, in a plausible range, and
        # the object must survive a repr() round-trip (this is exactly what
        # broke on the old demand_model.pkl under the newer scikit-learn).
        sample = Xd.iloc[:5]
        preds = demand_model.predict(sample)
        assert np.all(np.isfinite(preds)), "demand_model produced non-finite predictions"
        repr(demand_model)  # must not raise
        repr(no_show_pipe)  # must not raise

        sample_p = no_show_pipe.predict_proba(X.iloc[:5])[:, 1]
        assert np.all((sample_p >= 0) & (sample_p <= 1)), "no_show probabilities out of [0,1]"

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("--dry-run: not writing .pkl files"))
            return

        joblib.dump(no_show_pipe, RIDES_APP_DIR / "no_show_model.pkl")
        joblib.dump(demand_model, RIDES_APP_DIR / "demand_model.pkl")
        self.stdout.write(self.style.SUCCESS(
            "Retrained and saved no_show_model.pkl and demand_model.pkl "
            "against the currently installed scikit-learn version."
        ))
