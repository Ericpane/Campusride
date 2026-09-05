"""Shared-fare pricing for revenue-maximizing trip plans."""
from .geofence import haversine_distance
from .constants import DEFAULT_FARE_PER_KM

def _fare_per_km():
    try:
        from .ml_service import get_calibration
        cal = get_calibration()
        v = cal.get("fare_per_km")
        if v and float(v) > 0:
            return float(v)
    except Exception:
        pass
    return DEFAULT_FARE_PER_KM


def leg_fare(lat1, lng1, lat2, lng2):
    """Fare for one OD leg (₦)."""
    distance_km = haversine_distance(lat1, lng1, lat2, lng2) / 1000.0
    total = distance_km * _fare_per_km()
    return {
        "total_fare": round(total, 2),
        "per_rider_share": round(total, 2),
        "distance_km": round(distance_km, 2),
    }


def calculate_fare(pickup_lat, pickup_lng, drop_lat, drop_lng):
    """Back-compat alias used by older call sites."""
    return leg_fare(pickup_lat, pickup_lng, drop_lat, drop_lng)


def trip_revenue_pool(requests):
    """
    Total trip revenue pot (₦) for a set of RideRequest-like objects.
    MVP rule: sum of each rider's direct pickup→destination leg fare,
    then the pot is what gets shared — optimizer maximizes this total.
    """
    pool = 0.0
    for req in requests:
        dest = req.rider_destination
        info = leg_fare(req.pickup_lat, req.pickup_lng, dest.latitude, dest.longitude)
        pool += info["total_fare"]
    return round(pool, 2)


def shared_shares(total_pool, num_riders):
    """Split trip pot equally among paying riders."""
    if num_riders <= 0:
        return 0.0
    return round(float(total_pool) / num_riders, 2)


def calculate_fare_for_share(total_fare, num_riders):
    return shared_shares(total_fare, num_riders)


def expected_revenue(requests, use_p_board=True):
    """
    Expected collected revenue ≈ sum(leg_fare_i * p_board_i).
    Used as Optimize objective (maximize).
    """
    total = 0.0
    for req in requests:
        dest = req.rider_destination
        fare = leg_fare(req.pickup_lat, req.pickup_lng, dest.latitude, dest.longitude)["total_fare"]
        p = float(getattr(req, "p_board", 0.5) or 0.5) if use_p_board else 1.0
        p = max(0.05, min(1.0, p))
        total += fare * p
    return round(total, 2)
