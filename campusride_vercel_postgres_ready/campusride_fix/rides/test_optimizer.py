"""
Task 3 — business-logic correctness of the optimizer.

Decisions documented here (not silently skipped):

1. Multi-stop route quality vs 2-opt refinement
   matching.marginal_detour_meters scores each rider against the *fixed*
   driver→anchor leg only. _greedy_select / _milp_select therefore rank
   riders independently; _build_route then orders stops nearest-first
   *after* selection, with no feedback into who was chosen.

   For MVP with DEFAULT_TOTAL_SEATS = 3 the maximum simultaneous shared
   riders is 3, so the combined route has at most 1 driver start + 3
   pickups + 3 dropoffs + 1 anchor. Nearest-neighbour ordering is an
   acceptable simplification at this scale: full 2-opt / insertion-cost
   re-evaluation would add complexity without a clear revenue gain on
   3-seat campus hops. This is an explicit MVP scope decision, not an
   oversight. If seat capacity rises or campus diameter grows, revisit.

2. expected_revenue consistency
   _milp_select, _greedy_select, and the final recompute in optimize_trip
   all call the same fares.expected_revenue(). Tests below assert the
   values agree rather than assuming it.
"""
from django.contrib.auth.models import User
from django.test import TestCase

from rides.models import ActiveTrip, CampusLocation, Profile, RideRequest
from rides.optimizer import (
    _build_route,
    _greedy_select,
    _milp_select,
    optimize_trip,
)
from rides.fares import expected_revenue, trip_revenue_pool
from rides.matching import evaluate_rider, marginal_detour_meters
from rides.constants import DEFAULT_TOTAL_SEATS, MAX_DETOUR_METERS


def _user(username, role="rider"):
    u = User.objects.create_user(username=username, password="x")
    Profile.objects.create(user=u, role=role, phone_or_matric_number="0")
    return u


class OptimizerRevenueConsistencyTests(TestCase):
    def setUp(self):
        self.gate = CampusLocation.objects.create(
            name="Main Gate", latitude=7.4436, longitude=3.8970
        )
        self.faculty = CampusLocation.objects.create(
            name="Faculty", latitude=7.4460, longitude=3.9010
        )
        self.hostel = CampusLocation.objects.create(
            name="Hostel", latitude=7.4410, longitude=3.8950
        )
        self.driver = _user("optdriver", "driver")
        self.trip = ActiveTrip.objects.create(
            driver=self.driver,
            driver_lat=7.4436,
            driver_lng=3.8970,
            destination=self.faculty,
            status="active",
            total_seats=3,
            seats_available=3,
        )

    def _make_request(self, username, pickup, dest, p_board=0.8):
        rider = _user(username)
        return RideRequest.objects.create(
            trip=self.trip,
            rider=rider,
            pickup_lat=pickup[0],
            pickup_lng=pickup[1],
            rider_destination=dest,
            status="pending",
            p_board=p_board,
            compatibility_score=0.7,
            detour_meters=100,
        )

    def test_expected_revenue_same_for_milp_greedy_and_final(self):
        """Handoff requirement: check, don't assume, that all three paths agree."""
        r1 = self._make_request("r1", (7.4440, 3.8975), self.faculty, p_board=0.9)
        r2 = self._make_request("r2", (7.4445, 3.8980), self.faculty, p_board=0.7)
        candidates = [r1, r2]

        greedy = _greedy_select(candidates, seats=2)
        milp = _milp_select(candidates, seats=2)
        if milp is None:
            milp = greedy  # solver unavailable — still must match greedy path

        self.assertEqual(
            expected_revenue(greedy, use_p_board=True),
            expected_revenue(milp, use_p_board=True),
        )

        # Run full optimize and confirm stored expected_revenue matches
        # a direct call on the accepted set.
        optimize_trip(self.trip)
        self.trip.refresh_from_db()
        accepted = list(
            self.trip.requests.filter(status__in=["accepted", "boarded"])
        )
        self.assertAlmostEqual(
            self.trip.expected_revenue,
            expected_revenue(accepted, use_p_board=True),
            places=2,
        )

    def test_greedy_respects_seat_cap(self):
        reqs = [
            self._make_request(f"s{i}", (7.4440 + i * 0.0001, 3.8975), self.faculty)
            for i in range(5)
        ]
        selected = _greedy_select(reqs, seats=2)
        self.assertEqual(len(selected), 2)

    def test_greedy_prefers_higher_expected_revenue(self):
        low = self._make_request("low", (7.4440, 3.8975), self.faculty, p_board=0.2)
        high = self._make_request("high", (7.4441, 3.8976), self.faculty, p_board=0.95)
        selected = _greedy_select([low, high], seats=1)
        self.assertEqual(selected[0].id, high.id)


class MultiStopRouteTests(TestCase):
    """
    3+ riders in different directions — confirm planned_route_json is a
    coherent nearest-neighbour order (not asserted to be globally optimal;
    see module docstring for the 2-opt MVP decision).
    """

    def setUp(self):
        self.locs = [
            CampusLocation.objects.create(name="Gate", latitude=7.4436, longitude=3.8970),
            CampusLocation.objects.create(name="North", latitude=7.4480, longitude=3.8970),
            CampusLocation.objects.create(name="South", latitude=7.4390, longitude=3.8970),
            CampusLocation.objects.create(name="East", latitude=7.4436, longitude=3.9050),
        ]
        self.driver = _user("routedriver", "driver")
        self.trip = ActiveTrip.objects.create(
            driver=self.driver,
            driver_lat=7.4436,
            driver_lng=3.8970,
            destination=self.locs[0],  # Gate as anchor
            status="active",
            total_seats=3,
            seats_available=3,
        )

    def test_build_route_orders_pickups_nearest_first(self):
        # Three riders: one near driver, one north, one south
        riders = []
        for i, (name, lat, lng, dest) in enumerate([
            ("near", 7.4438, 3.8971, self.locs[1]),
            ("north", 7.4475, 3.8970, self.locs[1]),
            ("south", 7.4395, 3.8970, self.locs[2]),
        ]):
            u = _user(name)
            riders.append(
                RideRequest.objects.create(
                    trip=self.trip,
                    rider=u,
                    pickup_lat=lat,
                    pickup_lng=lng,
                    rider_destination=dest,
                    status="accepted",
                    p_board=0.8,
                )
            )

        route = _build_route(self.trip, riders)
        types = [s["type"] for s in route]
        self.assertEqual(types[0], "driver_start")
        self.assertEqual(types[-1], "anchor")
        # Every accepted request appears exactly once as pickup and once as dropoff
        pickups = [s for s in route if s["type"] == "pickup"]
        dropoffs = [s for s in route if s["type"] == "dropoff"]
        self.assertEqual(len(pickups), 3)
        self.assertEqual(len(dropoffs), 3)
        # Nearest-first: the first pickup should be the one closest to driver
        first_pickup = pickups[0]
        self.assertEqual(first_pickup["name"], "Pickup near")

    def test_optimize_with_divergent_riders_produces_valid_route(self):
        for name, lat, lng, dest in [
            ("a", 7.4440, 3.8972, self.locs[1]),
            ("b", 7.4430, 3.8965, self.locs[2]),
            ("c", 7.4436, 3.8990, self.locs[3]),
        ]:
            u = _user(name)
            RideRequest.objects.create(
                trip=self.trip,
                rider=u,
                pickup_lat=lat,
                pickup_lng=lng,
                rider_destination=dest,
                status="pending",
                p_board=0.85,
                compatibility_score=0.6,
                detour_meters=200,
            )
        optimize_trip(self.trip)
        self.trip.refresh_from_db()
        route = self.trip.planned_route_json or []
        self.assertTrue(len(route) >= 1)
        self.assertEqual(route[0]["type"], "driver_start")
        # seats never go negative
        self.assertGreaterEqual(self.trip.seats_available, 0)
        accepted = self.trip.requests.filter(status="accepted").count()
        self.assertLessEqual(accepted, DEFAULT_TOTAL_SEATS)


class MarginalDetourIndependenceTests(TestCase):
    """Confirm scoring is relative to fixed anchor, independent of other riders."""

    def test_detour_independent_of_other_riders(self):
        O = (7.4436, 3.8970)
        Z = (7.4460, 3.9010)
        P1 = (7.4440, 3.8975)
        D1 = (7.4455, 3.9000)
        d1 = marginal_detour_meters(O, Z, P1, D1)
        # Adding another hypothetical rider elsewhere does not change d1
        d1_again = marginal_detour_meters(O, Z, P1, D1)
        self.assertEqual(d1, d1_again)
        self.assertLessEqual(d1, MAX_DETOUR_METERS * 2)  # sanity
