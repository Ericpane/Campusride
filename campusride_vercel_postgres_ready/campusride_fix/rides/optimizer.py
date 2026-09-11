"""
Fuel-efficiency trip optimizer (OR core).

Primary objective: minimize fuel used per passenger-mile for ONE trip,
  subject to seat capacity and matching feasibility (pre-filtered).

Why not revenue-max: shared fares are internal transfers among riders and
the driver inside the same trip. Fuel burned on detours is a real external
cost the driver pays. Choosing packs that maximize passenger-km per unit
fuel is therefore the defensible system objective.

Implementation:
  1. Enumerate all feasible packs of size 1..seats (exact for capacity 3).
  2. Build a pickup-then-dropoff route; improve with precedence-respecting 2-opt.
  3. Score = -fuel_per_pax_km + fill_bonus - directness_penalty.
  4. Accept the best pack; equal-split the fare pool for settlement only.

Greedy / MILP revenue selectors remain as fallbacks if pack search fails.
"""
from __future__ import annotations

import itertools
import logging
from django.db import transaction

from .constants import (
    DEFAULT_TOTAL_SEATS,
    DIRECTNESS_PENALTY_WEIGHT,
    FILL_BONUS_PER_EXTRA_RIDER,
    FUEL_COST_PER_KM,
    MIN_PAX_KM,
    MIN_VEH_KM,
    TWO_OPT_MAX_ITERATIONS,
)
from .db_utils import retry_on_db_lock
from .fares import expected_revenue, trip_revenue_pool, shared_shares
from .geofence import haversine_distance
from .models import ActiveTrip

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Geometry helpers (metres from haversine_distance → km)
# ---------------------------------------------------------------------------

def _km(lat1, lng1, lat2, lng2):
    return haversine_distance(lat1, lng1, lat2, lng2) / 1000.0


def _route_length_km(route):
    if len(route) < 2:
        return 0.0
    total = 0.0
    for i in range(len(route) - 1):
        a, b = route[i], route[i + 1]
        total += _km(a["lat"], a["lng"], b["lat"], b["lng"])
    return total


# ---------------------------------------------------------------------------
# Routing: pickups (NN) then dropoffs (NN), then 2-opt with precedence
# ---------------------------------------------------------------------------

def _build_route(trip, selected_requests):
    """
    Ordered stops: driver → pickups (nearest-first) → dropoffs (nearest-first)
    → optional anchor. Prefer clustering all pickups before dropoffs so shared
    segments actually save vehicle-km (interleaving OD pairs wastes fuel).
    """
    route = [{
        "lat": trip.driver_lat,
        "lng": trip.driver_lng,
        "type": "driver_start",
        "name": "Driver",
        "request_id": None,
    }]
    if not selected_requests:
        if trip.destination_id:
            route.append({
                "lat": trip.destination.latitude,
                "lng": trip.destination.longitude,
                "type": "anchor",
                "name": trip.destination.name,
                "request_id": None,
            })
        return route

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

    remaining_drop = list(selected_requests)
    while remaining_drop:
        remaining_drop.sort(
            key=lambda r: haversine_distance(
                cur[0], cur[1], r.rider_destination.latitude, r.rider_destination.longitude
            )
        )
        r = remaining_drop.pop(0)
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
            "request_id": None,
        })

    return _two_opt_route(route, selected_requests)


def _precedence_ok(route, selected_requests):
    """Every rider's pickup must appear before their dropoff."""
    pos = {}
    for i, stop in enumerate(route):
        rid = stop.get("request_id")
        if rid is None:
            continue
        key = (rid, stop["type"])
        if key not in pos:
            pos[key] = i
    for r in selected_requests:
        pu = pos.get((r.id, "pickup"))
        do = pos.get((r.id, "dropoff"))
        if pu is not None and do is not None and pu > do:
            return False
    return True


def _two_opt_route(route, selected_requests, max_iter=TWO_OPT_MAX_ITERATIONS):
    """Intra-route 2-opt that never violates pickup-before-dropoff."""
    # Only reverse interior stops (keep driver_start and optional terminal anchor)
    if len(route) < 4:
        return route

    start = route[0]
    end = route[-1] if route[-1].get("type") == "anchor" else None
    middle = route[1:-1] if end is not None else route[1:]
    if len(middle) < 2:
        return route

    best = list(middle)
    best_len = _route_length_km([start] + best + ([end] if end else []))
    improved = True
    iterations = 0
    while improved and iterations < max_iter:
        improved = False
        iterations += 1
        for i in range(len(best) - 1):
            for j in range(i + 1, len(best)):
                cand = best[:i] + list(reversed(best[i : j + 1])) + best[j + 1 :]
                full = [start] + cand + ([end] if end else [])
                if not _precedence_ok(full, selected_requests):
                    continue
                cl = _route_length_km(full)
                if cl + 1e-9 < best_len:
                    best, best_len, improved = cand, cl, True
                    break
            if improved:
                break
    return [start] + best + ([end] if end else [])


# ---------------------------------------------------------------------------
# Pack scoring (fuel per passenger-mile)
# ---------------------------------------------------------------------------

def _pax_km(requests):
    """Expected direct passenger-km (weighted by p_board)."""
    total = 0.0
    for r in requests:
        dest = r.rider_destination
        d = _km(r.pickup_lat, r.pickup_lng, dest.latitude, dest.longitude)
        p = float(getattr(r, "p_board", 0.5) or 0.5)
        p = max(0.05, min(1.0, p))
        total += d * p
    return max(MIN_PAX_KM, total)


def score_pack(trip, requests):
    """
    Score one candidate pack. Higher is better.

    score = -fuel_per_pax_km + fill_bonus - directness_penalty

    fuel_per_pax_km = (veh_km * FUEL_COST_PER_KM) / pax_km
    """
    if not requests:
        return {
            "score": -999.0,
            "fuel_per_pax_km": 999.0,
            "veh_km": 0.0,
            "pax_km": 0.0,
            "route": [],
            "directness": 0.0,
        }

    route = _build_route(trip, requests)
    veh_km = max(MIN_VEH_KM, _route_length_km(route))
    pax = _pax_km(requests)
    fuel_per_pax_km = (veh_km * FUEL_COST_PER_KM) / pax

    ideal = sum(
        _km(r.pickup_lat, r.pickup_lng, r.rider_destination.latitude, r.rider_destination.longitude)
        for r in requests
    )
    directness = min(1.4, max(0.25, ideal / max(veh_km, 0.01)))
    directness_penalty = max(0.0, 1.0 - directness) * DIRECTNESS_PENALTY_WEIGHT
    fill_bonus = FILL_BONUS_PER_EXTRA_RIDER * max(0, len(requests) - 1)

    score = -fuel_per_pax_km + fill_bonus - directness_penalty
    return {
        "score": score,
        "fuel_per_pax_km": fuel_per_pax_km,
        "veh_km": veh_km,
        "pax_km": pax,
        "route": route,
        "directness": directness,
    }


def _enumerate_packs(candidates, seats):
    """All non-empty subsets of size ≤ seats (exact; seats ≤ 3 in production)."""
    seats = max(0, int(seats))
    if seats <= 0 or not candidates:
        return []
    packs = []
    n = len(candidates)
    # Cap combinatorial explosion if matching ever returns many pending
    max_n = min(n, 12)
    pool = candidates[:max_n]
    for k in range(1, min(seats, len(pool)) + 1):
        for combo in itertools.combinations(pool, k):
            packs.append(list(combo))
    return packs


def _select_best_pack(trip, candidates, seats):
    """
    Enumerate packs, score by fuel efficiency, return best pack + metrics.
    Falls back to empty if nothing scores.
    """
    packs = _enumerate_packs(candidates, seats)
    if not packs:
        return [], {"method": "empty", "fuel_per_pax_km": None, "veh_km": 0.0, "pax_km": 0.0}

    best = None
    best_meta = None
    for pack in packs:
        meta = score_pack(trip, pack)
        if best is None or meta["score"] > best_meta["score"]:
            best = pack
            best_meta = meta

    best_meta["method"] = "pack_fuel"
    return best or [], best_meta


# ---------------------------------------------------------------------------
# Legacy selectors (kept for tests / emergency fallback)
# ---------------------------------------------------------------------------

def _greedy_select(candidates, seats):
    """
    Greedy maximize expected revenue with seat limit.
    Kept as a fallback and for unit tests that assert seat-cap behaviour.
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


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

@retry_on_db_lock()
def optimize_trip(trip):
    """
    Select pending requests to minimize fuel per passenger-mile.
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
            accepted_existing = list(
                trip.requests.filter(status__in=["accepted", "boarded"]).select_related(
                    "rider_destination", "rider"
                )
            )
            route = _build_route(trip, accepted_existing)
            trip.planned_route_json = route
            trip.save(update_fields=["planned_route_json"])
            return []

        seats = max(0, trip.seats_available)

        # Primary: fuel-efficient pack enumeration
        selected, meta = _select_best_pack(trip, pending, seats)
        method = meta.get("method", "pack_fuel")

        # Fallback if pack search returned nothing usable
        if not selected and seats > 0 and pending:
            selected = _milp_select(pending, seats)
            method = "milp"
            if selected is None:
                selected = _greedy_select(pending, seats)
                method = "greedy"
            meta = score_pack(trip, selected) if selected else meta

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
        full_pool = trip_revenue_pool(all_accepted)
        full_share = shared_shares(full_pool, len(all_accepted)) if all_accepted else 0.0
        for req in all_accepted:
            req.per_rider_fare_share = full_share
            req.save(update_fields=["per_rider_fare_share"])

        trip.total_trip_fare_estimate = full_pool
        trip.expected_revenue = expected_revenue(all_accepted, use_p_board=True)
        trip.planned_route_json = _build_route(trip, all_accepted)

        fuel_ppk = meta.get("fuel_per_pax_km")
        veh_km = meta.get("veh_km")
        pax_km = meta.get("pax_km")
        names = ", ".join(r.rider.username for r in selected) or "none"
        fuel_str = f"{fuel_ppk:.4f}" if fuel_ppk is not None else "n/a"
        trip.plan_explanation = (
            f"Method={method}; accepted [{names}]; "
            f"fuel/pax-km={fuel_str}; veh_km={veh_km}; pax_km={pax_km}; "
            f"pool=₦{full_pool}; expected=₦{trip.expected_revenue}; "
            f"share=₦{full_share}/rider; seats_left={trip.seats_available}"
        )
        trip.save(update_fields=[
            "seats_available", "status", "total_trip_fare_estimate",
            "expected_revenue", "planned_route_json", "plan_explanation",
        ])

    logger.info(
        "Trip %s optimized (%s): accepted %s, fuel/pax-km=%s, expected ₦%s",
        trip.id, method, [r.id for r in selected],
        meta.get("fuel_per_pax_km"), trip.expected_revenue,
    )
    return [r.id for r in selected]
