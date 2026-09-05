"""
End-to-end plan_trip pipeline: demand → anchor → matching context → optimize.
"""
from __future__ import annotations

import logging
from django.db import transaction
from django.utils import timezone

from .models import ActiveTrip, CampusLocation, RideRequest
from .constants import DEFAULT_TOTAL_SEATS
from .db_utils import retry_on_db_lock
from .ml_service import choose_anchor_location, predict_demand, predict_p_board
from .matching import evaluate_rider
from .geofence import haversine_distance
from .optimizer import optimize_trip
from .fares import leg_fare

logger = logging.getLogger(__name__)


def _anchor_coords(trip):
    if trip.destination_id:
        d = trip.destination
        return (d.latitude, d.longitude)
    # fallback: slight offset from driver so geometry still works
    return (trip.driver_lat + 0.01, trip.driver_lng + 0.01)


def enrich_request_scores(trip, ride_req):
    """Matching + ML no-show on a single request; may mark infeasible via score=0."""
    O = (trip.driver_lat, trip.driver_lng)
    Z = _anchor_coords(trip)
    P = (ride_req.pickup_lat, ride_req.pickup_lng)
    D = (ride_req.rider_destination.latitude, ride_req.rider_destination.longitude)

    ev = evaluate_rider(O, Z, P, D)
    ride_req.compatibility_score = ev["score"] if ev["ok"] else 0.0
    ride_req.detour_meters = ev["detour_m"]

    now = timezone.now()
    wait_min = max(0.0, (now - ride_req.requested_at).total_seconds() / 60.0) if ride_req.requested_at else 0.0
    dist_km = haversine_distance(O[0], O[1], P[0], P[1]) / 1000.0
    ride_req.p_board = predict_p_board(
        waiting_minutes=wait_min,
        detour_km=ev["detour_m"] / 1000.0,
        hour_of_day=now.hour,
        day_of_week=now.weekday(),
        dist_to_rider_km=dist_km,
    )
    ride_req.save(update_fields=["compatibility_score", "detour_meters", "p_board"])
    return ev["ok"]


@retry_on_db_lock()
def _score_pending_requests(trip):
    """
    Scores every currently-pending request for `trip` and declines any that
    turn out infeasible, in one transaction. This used to run as a series of
    unwrapped, individually-autocommitted writes (one per request) outside
    any lock -- fine for a single caller, but under concurrent
    api_request_seat calls for the same trip, two callers' scoring loops
    could interleave their writes and hit SQLite's "database table is
    locked" (SQLite has no row-level locking; see optimizer.optimize_trip
    for the fuller explanation). Wrapping the whole loop in one atomic block
    with retry_on_db_lock gives each caller's scoring pass a clean,
    all-or-nothing attempt, same as optimize_trip already has.
    """
    with transaction.atomic():
        pending = trip.requests.filter(status="pending").select_related("rider_destination")
        for req in pending:
            ok = enrich_request_scores(trip, req)
            if not ok:
                req.status = "declined"
                req.save(update_fields=["status"])


def plan_trip(trip: ActiveTrip):
    """
    Full OR pipeline for one trip:
    1) Ensure system anchor from demand if missing
    2) Score all pending with Matching + ML
    3) Drop infeasible pending (decline)
    4) Optimize revenue-maximizing accept set + route
    """
    if not trip.destination_id:
        locs = CampusLocation.objects.all()
        anchor = choose_anchor_location(
            locs, driver_lat=trip.driver_lat, driver_lng=trip.driver_lng
        )
        if anchor:
            trip.destination = anchor
            trip.save(update_fields=["destination"])

    _score_pending_requests(trip)

    accepted_ids = optimize_trip(trip)
    trip.refresh_from_db()
    logger.info(
        "plan_trip %s done: demand_hint=%s explanation=%s",
        trip.id,
        predict_demand(timezone.now().hour, timezone.now().weekday()),
        trip.plan_explanation,
    )
    return accepted_ids


def create_trip_for_driver(driver, lat: float, lng: float) -> ActiveTrip:
    """Driver go-online: no destination picker — system owns the plan."""
    locs = list(CampusLocation.objects.all())
    anchor = choose_anchor_location(locs, driver_lat=lat, driver_lng=lng)
    # IMPORTANT: persist the device coordinates exactly as received.
    # Visual marker separation belongs in the UI, never in the database.
    lat = float(lat)
    lng = float(lng)
    trip = ActiveTrip.objects.create(
        driver=driver,
        driver_lat=lat,
        driver_lng=lng,
        destination=anchor,
        status="active",
        total_seats=DEFAULT_TOTAL_SEATS,
        seats_available=DEFAULT_TOTAL_SEATS,
        total_trip_fare_estimate=0,
        expected_revenue=0,
        planned_route_json=[],
        plan_explanation="Awaiting riders — system will maximize revenue per trip.",
    )
    plan_trip(trip)
    return trip
