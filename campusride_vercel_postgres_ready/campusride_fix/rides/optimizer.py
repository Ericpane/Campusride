"""
Revenue-maximizing trip optimizer (OR core).

Objective: maximize expected shared-trip revenue for ONE trip
  subject to seat capacity and matching feasibility (pre-filtered).

Uses greedy set selection by default (always available).
Optionally uses Pyomo MILP when CBC/GLPK is installed.
"""
from __future__ import annotations

import logging
from django.db import transaction
from django.utils import timezone

from .constants import DEFAULT_TOTAL_SEATS
from .db_utils import retry_on_db_lock
from .fares import expected_revenue, trip_revenue_pool, shared_shares
from .geofence import haversine_distance
from .models import ActiveTrip

logger = logging.getLogger(__name__)


def _build_route(trip, selected_requests):
    """Simple ordered stops: driver → pickups (nearest-first) → drops → anchor."""
    route = [{
        "lat": trip.driver_lat,
        "lng": trip.driver_lng,
        "type": "driver_start",
        "name": "Driver",
    }]
    remaining = list(selected_requests)
    cur = (trip.driver_lat, trip.driver_lng)
    while remaining:
        remaining.sort(
            key=lambda r: haversine_distance(cur[0], cur[1], r.pickup_lat, r.pickup_lng)
        )
        r = remaining.pop(0)
        route.append({
            "lat": r.pickup_lat,
            "lng": r.pickup_lng,
            "type": "pickup",
            "request_id": r.id,
            "name": f"Pickup {r.rider.username}",
        })
        cur = (r.pickup_lat, r.pickup_lng)
        dest = r.rider_destination
        route.append({
            "lat": dest.latitude,
            "lng": dest.longitude,
            "type": "dropoff",
            "request_id": r.id,
            "name": dest.name,
        })
        cur = (dest.latitude, dest.longitude)
    if trip.destination_id:
        route.append({
            "lat": trip.destination.latitude,
            "lng": trip.destination.longitude,
            "type": "anchor",
            "name": trip.destination.name,
        })
    return route


def _greedy_select(candidates, seats):
    """
    Greedy maximize expected revenue with seat limit.
    candidates: list of RideRequest (feasible).
    """
    ranked = sorted(
        candidates,
        key=lambda r: expected_revenue([r], use_p_board=True),
        reverse=True,
    )
    return ranked[: max(0, seats)]


def _milp_select(candidates, seats):
    """Binary ILP max sum expected_revenue_i * x_i s.t. sum x <= seats."""
    try:
        from pyomo.environ import (
            ConcreteModel, Var, Binary, RangeSet, Objective, Constraint,
            SolverFactory, maximize,
        )
    except ImportError:
        return None

    n = len(candidates)
    if n == 0 or seats <= 0:
        return []

    values = [expected_revenue([r], use_p_board=True) for r in candidates]
    model = ConcreteModel()
    model.I = RangeSet(n)
    model.x = Var(model.I, domain=Binary)
    model.obj = Objective(
        expr=sum(values[i - 1] * model.x[i] for i in model.I),
        sense=maximize,
    )
    model.cap = Constraint(expr=sum(model.x[i] for i in model.I) <= seats)

    for name in ("cbc", "glpk", "scip"):
        try:
            solver = SolverFactory(name)
            if solver is None or not solver.available(exception_flag=False):
                continue
            solver.solve(model, tee=False)
            selected = [
                candidates[i - 1]
                for i in model.I
                if model.x[i].value is not None and model.x[i].value > 0.5
            ]
            return selected
        except Exception as e:
            logger.warning("MILP solver %s failed: %s", name, e)
    return None


@retry_on_db_lock()
def optimize_trip(trip):
    """
    Select pending requests to maximize expected revenue per trip.
    Auto-accepts selected riders, updates seats, route, fare shares.
    Returns list of accepted request IDs.

    Concurrency note: the entire read-select-write cycle happens inside one
    transaction.atomic() block, with the ActiveTrip row locked via
    select_for_update() at the top. This closes the race where two requests
    for the same trip's last seat(s) could both read a stale
    seats_available, both compute a selection based on that stale value,
    and both write an accepted status back -- oversubscribing the vehicle.

    select_for_update() is a real row lock on Postgres/MySQL. On SQLite
    (this project's default backend) Django silently downgrades it to a
    no-op -- SQLite has no row-level locking -- but the surrounding
    transaction.atomic() still gets SQLite's own database-level write lock
    for the duration of the block, so concurrent optimize_trip() calls for
    the same (or different) trips still serialize correctly; the second
    caller blocks until the first commits, then re-reads the now-updated
    seats_available. Django's sqlite3 backend has a 5-second busy timeout by
    default, which is enough for this block (a handful of small UPDATEs);
    under heavier concurrent load a longer timeout could still be needed
    (see DATABASES OPTIONS in settings.py) or a move to Postgres.
    """
    with transaction.atomic():
        # Lock (Postgres/MySQL) / serialize (SQLite) on this trip row before
        # reading anything we're about to act on, so a concurrent
        # optimize_trip() call for the same trip can't interleave with ours.
        trip = ActiveTrip.objects.select_for_update().get(pk=trip.pk)

        pending = list(
            trip.requests.filter(status="pending").select_related("rider_destination", "rider")
        )
        if not pending:
            logger.info("No pending requests for trip %s", trip.id)
            # Still refresh route with already accepted
            accepted_existing = list(
                trip.requests.filter(status__in=["accepted", "boarded"]).select_related(
                    "rider_destination", "rider"
                )
            )
            route = _build_route(trip, accepted_existing)
            trip.planned_route_json = route
            trip.save(update_fields=["planned_route_json"])
            return []

        # Freshly-read (under lock) seat count -- this is the value that
        # matters, not whatever the caller's `trip` object had before we
        # re-fetched it above.
        seats = max(0, trip.seats_available)
        # Prefer MILP when available; always fall back to greedy
        selected = _milp_select(pending, seats)
        method = "milp"
        if selected is None:
            selected = _greedy_select(pending, seats)
            method = "greedy"

        pool = trip_revenue_pool(selected)
        n = len(selected)
        share = shared_shares(pool, n) if n else 0.0

        for req in selected:
            if req.status != "pending":
                continue
            req.status = "accepted"
            req.per_rider_fare_share = share
            req.save(update_fields=["status", "per_rider_fare_share"])

        num = len(selected)
        if num > 0:
            trip.seats_available = max(0, trip.seats_available - num)
            if trip.seats_available <= 0:
                trip.seats_available = 0
                trip.status = "full"
            else:
                trip.status = "active"

        all_accepted = list(
            trip.requests.filter(status__in=["accepted", "boarded"]).select_related(
                "rider_destination", "rider"
            )
        )
        # Recompute shares across all accepted for true shared-fare story
        full_pool = trip_revenue_pool(all_accepted)
        full_share = shared_shares(full_pool, len(all_accepted)) if all_accepted else 0.0
        for req in all_accepted:
            req.per_rider_fare_share = full_share
            req.save(update_fields=["per_rider_fare_share"])

        trip.total_trip_fare_estimate = full_pool
        trip.expected_revenue = expected_revenue(all_accepted, use_p_board=True)
        trip.planned_route_json = _build_route(trip, all_accepted)
        names = ", ".join(r.rider.username for r in selected) or "none"
        trip.plan_explanation = (
            f"Method={method}; accepted [{names}]; "
            f"pool=₦{full_pool}; expected=₦{trip.expected_revenue}; "
            f"share=₦{full_share}/rider; seats_left={trip.seats_available}"
        )
        trip.save(update_fields=[
            "seats_available", "status", "total_trip_fare_estimate",
            "expected_revenue", "planned_route_json", "plan_explanation",
        ])

    logger.info(
        "Trip %s optimized (%s): accepted %s, expected ₦%s",
        trip.id, method, [r.id for r in selected], trip.expected_revenue,
    )
    return [r.id for r in selected]
