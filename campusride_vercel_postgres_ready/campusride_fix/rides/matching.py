from math import cos
from .geofence import haversine_distance
from .constants import MAX_DETOUR_METERS, MIN_ALIGNMENT_THRESHOLD

# Below this length (meters), the driver->anchor reference vector is too
# short to reliably tell "direction" from GPS-noise-scale jitter. Below this
# threshold we skip the alignment check rather than let a near-zero-length
# vector produce a numerically unstable (and often falsely-negative) score.
MIN_REFERENCE_VECTOR_METERS = 50


def distance_point_to_segment(px, py, ax, ay, bx, by):
    """Approximate perpendicular distance from point P to segment AB (meters)."""
    def to_meters(dlat, dlng, lat_ref):
        lat_rad = lat_ref * 3.14159 / 180
        return (dlat * 111000, dlng * 111000 * cos(lat_rad))

    lat_ref = (ay + by) / 2
    ax_m, ay_m = 0.0, 0.0
    bx_m, by_m = to_meters(bx - ax, by - ay, lat_ref)
    px_m, py_m = to_meters(px - ax, py - ay, lat_ref)

    abx, aby = bx_m - ax_m, by_m - ay_m
    denom = abx * abx + aby * aby + 1e-9
    t = ((px_m - ax_m) * abx + (py_m - ay_m) * aby) / denom
    t = max(0.0, min(1.0, t))
    proj_x = ax_m + t * abx
    proj_y = ay_m + t * aby
    return ((px_m - proj_x) ** 2 + (py_m - proj_y) ** 2) ** 0.5


def direction_alignment(ax, ay, bx, by, cx, cy, dx, dy):
    import math
    ab = (bx - ax, by - ay)
    cd = (dx - cx, dy - cy)
    dot = ab[0] * cd[0] + ab[1] * cd[1]
    mag_ab = math.hypot(ab[0], ab[1])
    mag_cd = math.hypot(cd[0], cd[1])
    if mag_ab == 0 or mag_cd == 0:
        return 0.0
    return dot / (mag_ab * mag_cd)


def marginal_detour_meters(driver_start, driver_dest, rider_pickup, rider_dest):
    """O→P→D→Z minus O→Z (meters)."""
    O, Z = driver_start, driver_dest
    P, D = rider_pickup, rider_dest
    dist_OZ = haversine_distance(O[0], O[1], Z[0], Z[1])
    dist_OP = haversine_distance(O[0], O[1], P[0], P[1])
    dist_PD = haversine_distance(P[0], P[1], D[0], D[1])
    dist_DZ = haversine_distance(D[0], D[1], Z[0], Z[1])
    return max(0.0, dist_OP + dist_PD + dist_DZ - dist_OZ)


def evaluate_rider(driver_start, driver_dest, rider_pickup, rider_dest):
    """
    Hard feasibility + score for inserting rider into planned OD/anchor path.
    Returns dict: ok, score, detour_m, alignment
    """
    detour = marginal_detour_meters(driver_start, driver_dest, rider_pickup, rider_dest)
    if detour > MAX_DETOUR_METERS:
        # also try segment distance as softer campus check
        seg = distance_point_to_segment(
            rider_pickup[0], rider_pickup[1],
            driver_start[0], driver_start[1],
            driver_dest[0], driver_dest[1],
        )
        if seg > MAX_DETOUR_METERS:
            return {"ok": False, "score": 0.0, "detour_m": detour, "alignment": 0.0}

    reference_length_m = haversine_distance(
        driver_start[0], driver_start[1], driver_dest[0], driver_dest[1]
    )
    if reference_length_m < MIN_REFERENCE_VECTOR_METERS:
        # The driver->anchor vector is too short to give a meaningful
        # direction (e.g. anchor picked essentially on top of the driver).
        # Fall back to the detour-only score instead of penalizing the rider
        # for a reference direction that doesn't actually mean anything.
        alignment = 0.0
        score = 1 - min(detour, MAX_DETOUR_METERS) / MAX_DETOUR_METERS
        return {"ok": True, "score": score, "detour_m": detour, "alignment": alignment}

    alignment = direction_alignment(
        driver_start[0], driver_start[1], driver_dest[0], driver_dest[1],
        rider_pickup[0], rider_pickup[1], rider_dest[0], rider_dest[1],
    )
    if alignment < MIN_ALIGNMENT_THRESHOLD:
        return {"ok": False, "score": 0.0, "detour_m": detour, "alignment": alignment}

    score = (1 - min(detour, MAX_DETOUR_METERS) / MAX_DETOUR_METERS) * 0.5 + max(alignment, 0) * 0.5
    return {"ok": True, "score": score, "detour_m": detour, "alignment": alignment}


def compatibility_score(driver_start, driver_dest, rider_pickup, rider_dest):
    """Back-compat: returns score or None."""
    r = evaluate_rider(driver_start, driver_dest, rider_pickup, rider_dest)
    return r["score"] if r["ok"] else None
