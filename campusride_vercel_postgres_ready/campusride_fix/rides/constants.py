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

# ---------------------------------------------------------------------------
# Fuel-efficiency objective (primary)
# Minimize fuel used per passenger-mile. Revenue remains a settlement rule
# (equal split of the fare pool), not the thing being maximized.
# ---------------------------------------------------------------------------
# Approximate keke fuel cost in ₦ per vehicle-km (calibrate from local pump
# price × consumption later; 40 ₦/km is a conservative campus placeholder).
FUEL_COST_PER_KM = 40.0
# Soft bonuses / penalties in the same numeric units as fuel_per_pax_km so
# a fuller pack can beat a slightly leaner empty-ish pack.
FILL_BONUS_PER_EXTRA_RIDER = 0.02
DIRECTNESS_PENALTY_WEIGHT = 0.30
# Floor on route length so degenerate zero-distance packs do not explode
# the reciprocal objective.
MIN_VEH_KM = 0.05
MIN_PAX_KM = 0.05
# 2-opt local improvement budget (stops are few; this is cheap).
TWO_OPT_MAX_ITERATIONS = 20
