"""Shared thresholds for matching + optimization (single source of truth)."""

# Geometry
MAX_DETOUR_METERS = 800          # hard feasibility (aligned for plan inserts)
MIN_ALIGNMENT_THRESHOLD = 0.55  # slightly looser than old 0.7 for multi-stop plans
# Anchor must be at least this far from the driver's own position, otherwise
# the driver->anchor reference vector used for direction_alignment is too
# short/noisy to mean anything (see ml_service.choose_anchor_location and
# matching.direction_alignment).
MIN_ANCHOR_DISTANCE_METERS = 250

# Wait priority (soft)
WAIT_TIME_CAP_SECONDS = 300
PRIORITY_STRENGTH = 0.5

# Vehicle
DEFAULT_TOTAL_SEATS = 3

# Fares fallback if calibration missing
DEFAULT_FARE_PER_KM = 133.33
