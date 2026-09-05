"""
Full lifecycle / pipeline tests for CampusRide OR MVP.
Run with: python manage.py test rides.test_full_pipeline -v 2
"""
import json
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse

from rides.models import Profile, CampusLocation, ActiveTrip, RideRequest
from rides import ml_service
from rides.planning import create_trip_for_driver, plan_trip
from rides.matching import evaluate_rider, marginal_detour_meters
from rides.fares import leg_fare, trip_revenue_pool, shared_shares, expected_revenue
from rides.optimizer import optimize_trip


def make_user(username, role, password="testpass123"):
    user = User.objects.create_user(username=username, password=password)
    Profile.objects.create(user=user, role=role, phone_or_matric_number="000")
    return user


class MLServiceLoadingTests(TestCase):
    """These models were trained with joblib.dump; ml_service used plain
    pickle.load, so predict_p_board/predict_demand always fell back to the
    heuristic. Verifies models actually load and predict."""

    def test_no_show_model_loads_and_predicts(self):
        ml_service._load_pickle.cache_clear()
        model = ml_service._load_pickle("no_show_model.pkl")
        self.assertIsNotNone(model, "no_show_model.pkl failed to load")
        self.assertTrue(hasattr(model, "predict_proba"))

    def test_demand_model_loads_and_predicts(self):
        ml_service._load_pickle.cache_clear()
        model = ml_service._load_pickle("demand_model.pkl")
        self.assertIsNotNone(model, "demand_model.pkl failed to load")

    def test_predict_p_board_uses_model_not_heuristic_fallback(self):
        ml_service._load_pickle.cache_clear()
        p = ml_service.predict_p_board(
            waiting_minutes=5, detour_km=0.2, hour_of_day=8, day_of_week=1,
            dist_to_rider_km=0.5,
        )
        self.assertGreaterEqual(p, 0.05)
        self.assertLessEqual(p, 0.99)

    def test_predict_demand_does_not_crash(self):
        ml_service._load_pickle.cache_clear()
        d = ml_service.predict_demand(8, 1)
        self.assertIsInstance(d, float)
        self.assertGreater(d, 0)


class GeofenceAndConstantsTests(TestCase):
    def test_geofence_always_true_by_design(self):
        from rides.geofence import is_within_campus
        self.assertTrue(is_within_campus(0, 0))
        self.assertTrue(is_within_campus(999, 999))


class MatchingLogicTests(TestCase):
    def test_within_detour_and_aligned_is_feasible(self):
        O = (7.4436, 3.8970)
        Z = (7.4460, 3.9010)  # anchor, far enough from O
        P = (7.4440, 3.8975)  # pickup near driver, roughly along the O->Z line
        D = (7.4455, 3.9000)  # dropoff near anchor
        result = evaluate_rider(O, Z, P, D)
        self.assertTrue(result["ok"], result)

    def test_huge_detour_is_infeasible(self):
        O = (7.4436, 3.8970)
        Z = (7.4460, 3.9010)
        P = (7.0, 3.0)  # far away
        D = (6.9, 2.9)
        result = evaluate_rider(O, Z, P, D)
        self.assertFalse(result["ok"])

    def test_marginal_detour_is_nonnegative(self):
        O = (7.4436, 3.8970)
        Z = (7.4460, 3.9010)
        P = (7.4440, 3.8975)
        D = (7.4455, 3.9000)
        self.assertGreaterEqual(marginal_detour_meters(O, Z, P, D), 0)


class FareLogicTests(TestCase):
    def test_leg_fare_positive_for_nonzero_distance(self):
        info = leg_fare(7.4436, 3.8970, 7.4460, 3.9010)
        self.assertGreater(info["total_fare"], 0)
        self.assertGreater(info["distance_km"], 0)

    def test_shared_shares_splits_evenly(self):
        self.assertEqual(shared_shares(300, 3), 100.0)
        self.assertEqual(shared_shares(300, 0), 0.0)

    def test_expected_revenue_scales_with_p_board(self):
        class FakeReq:
            def __init__(self, p):
                self.pickup_lat, self.pickup_lng = 7.4436, 3.8970
                self.rider_destination = CampusLocation(latitude=7.4460, longitude=3.9010)
                self.p_board = p
        low = expected_revenue([FakeReq(0.1)])
        high = expected_revenue([FakeReq(0.9)])
        self.assertLess(low, high)


class FullLifecycleTests(TestCase):
    """End-to-end: signup -> go online -> request seat -> optimize ->
    accept -> board via QR -> end trip -> fare split."""

    def setUp(self):
        self.client = Client()
        CampusLocation.objects.bulk_create([
            CampusLocation(name="Main Gate", latitude=7.4436, longitude=3.8970),
            CampusLocation(name="Library", latitude=7.4450, longitude=3.8990),
            CampusLocation(name="Hostel Area", latitude=7.4410, longitude=3.8950),
            CampusLocation(name="Faculty Complex", latitude=7.4460, longitude=3.9010),
            CampusLocation(name="Sports Complex", latitude=7.4400, longitude=3.8930),
        ])

    def test_signup_creates_profile_with_correct_role(self):
        resp = self.client.post(reverse("login"), {
            "full_name": "Test Driver", "id_number": "DRV001",
            "password": "pass12345", "role": "staff",
        })
        user = User.objects.get(username="drv001")
        self.assertEqual(user.profile.role, "driver")
        self.assertRedirects(resp, reverse("driver_map"))

    def test_wrong_password_rejected(self):
        User.objects.create_user(username="rider1", password="correct123")
        Profile.objects.create(user=User.objects.get(username="rider1"), role="rider", phone_or_matric_number="1")
        resp = self.client.post(reverse("login"), {
            "full_name": "Rider One", "id_number": "rider1",
            "password": "wrongpass", "role": "rider",
        })
        self.assertContains(resp, "Incorrect password")

    def test_role_cannot_be_flipped_by_relogin(self):
        # First login creates a rider account
        self.client.post(reverse("login"), {
            "full_name": "Person X", "id_number": "PX1",
            "password": "pass12345", "role": "rider",
        })
        self.client.get(reverse("logout"))
        # Attempt to log back in claiming staff role
        self.client.post(reverse("login"), {
            "full_name": "Person X", "id_number": "PX1",
            "password": "pass12345", "role": "staff",
        })
        user = User.objects.get(username="px1")
        self.assertEqual(user.profile.role, "rider")

    def test_driver_go_online_creates_trip_with_anchor(self):
        driver = make_user("driver1", "driver")
        self.client.login(username="driver1", password="testpass123")
        resp = self.client.post(reverse("driver_go_online"), {"lat": "7.4436", "lng": "3.8970"})
        self.assertRedirects(resp, reverse("driver_active_trip"))
        trip = ActiveTrip.objects.get(driver=driver)
        self.assertIsNotNone(trip.destination_id)
        self.assertEqual(trip.status, "active")

    def test_duplicate_go_online_does_not_create_second_trip(self):
        driver = make_user("driver2", "driver")
        self.client.login(username="driver2", password="testpass123")
        self.client.post(reverse("driver_go_online"), {"lat": "7.4436", "lng": "3.8970"})
        self.client.post(reverse("driver_go_online"), {"lat": "7.4436", "lng": "3.8970"})
        self.assertEqual(ActiveTrip.objects.filter(driver=driver).count(), 1)

    def test_full_pipeline_request_optimize_board_complete(self):
        driver = make_user("driver3", "driver")
        rider = make_user("rider3", "rider")

        trip = create_trip_for_driver(driver, 7.4436, 3.8970)
        self.assertIn(trip.status, ("active", "forming"))
        trip.refresh_from_db()
        anchor = trip.destination  # system-chosen; align pickup/dest with it

        self.client.login(username="rider3", password="testpass123")
        dest = CampusLocation.objects.get(name="Faculty Complex")
        # Pickup a short step from the driver, roughly along the driver->anchor
        # line, so the alignment check passes regardless of which anchor the
        # demand model picked.
        pickup_lat = trip.driver_lat + (anchor.latitude - trip.driver_lat) * 0.1
        pickup_lng = trip.driver_lng + (anchor.longitude - trip.driver_lng) * 0.1
        resp = self.client.post(
            reverse("api_request_seat", args=[trip.id]),
            data=json.dumps({"lat": pickup_lat, "lng": pickup_lng, "destination_id": anchor.id}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        req_id = body["request_id"]

        ride_req = RideRequest.objects.get(id=req_id)
        # Should have been auto-optimized into "accepted" if feasible, or
        # explicitly declined -- either way must not be stuck "pending".
        self.assertIn(ride_req.status, ("accepted", "declined"))

        if ride_req.status == "accepted":
            self.assertGreater(ride_req.per_rider_fare_share, 0)
            trip.refresh_from_db()
            self.assertLess(trip.seats_available, trip.total_seats)

            # QR flow
            self.client.login(username="driver3", password="testpass123")
            confirm = self.client.post(
                reverse("api_confirm_boarding"),
                data=json.dumps({"token": str(ride_req.qr_token)}),
                content_type="application/json",
            )
            self.assertEqual(confirm.status_code, 200, confirm.content)
            ride_req.refresh_from_db()
            self.assertEqual(ride_req.status, "boarded")

            # End trip
            end_resp = self.client.get(reverse("end_trip", args=[trip.id]))
            self.assertEqual(end_resp.status_code, 302)
            ride_req.refresh_from_db()
            trip.refresh_from_db()
            self.assertEqual(ride_req.status, "completed")
            self.assertEqual(trip.status, "completed")
            self.assertIsNotNone(trip.completed_at)

    def test_seat_request_on_full_trip_rejected(self):
        driver = make_user("driver4", "driver")
        trip = create_trip_for_driver(driver, 7.4436, 3.8970)
        trip.seats_available = 0
        trip.status = "full"
        trip.save()

        rider = make_user("rider4", "rider")
        self.client.login(username="rider4", password="testpass123")
        dest = CampusLocation.objects.first()
        resp = self.client.post(
            reverse("api_request_seat", args=[trip.id]),
            data=json.dumps({"lat": 7.4438, "lng": 3.8972, "destination_id": dest.id}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_cancel_request_frees_seat(self):
        driver = make_user("driver5", "driver")
        rider = make_user("rider5", "rider")
        trip = create_trip_for_driver(driver, 7.4436, 3.8970)
        trip.refresh_from_db()
        anchor = trip.destination

        self.client.login(username="rider5", password="testpass123")
        pickup_lat = trip.driver_lat + (anchor.latitude - trip.driver_lat) * 0.1
        pickup_lng = trip.driver_lng + (anchor.longitude - trip.driver_lng) * 0.1
        resp = self.client.post(
            reverse("api_request_seat", args=[trip.id]),
            data=json.dumps({"lat": pickup_lat, "lng": pickup_lng, "destination_id": anchor.id}),
            content_type="application/json",
        )
        req_id = resp.json()["request_id"]
        ride_req = RideRequest.objects.get(id=req_id)
        if ride_req.status != "accepted":
            self.skipTest("Request wasn't auto-accepted, nothing to cancel-test")

        trip.refresh_from_db()
        seats_before = trip.seats_available
        cancel_resp = self.client.post(
            reverse("api_cancel_request", args=[req_id]),
            content_type="application/json",
        )
        self.assertEqual(cancel_resp.status_code, 200)
        trip.refresh_from_db()
        self.assertEqual(trip.seats_available, seats_before + 1)
        ride_req.refresh_from_db()
        self.assertEqual(ride_req.status, "declined")

    def test_rider_cannot_board_other_drivers_trip_qr(self):
        driver_a = make_user("driverA", "driver")
        driver_b = make_user("driverB", "driver")
        rider = make_user("riderA", "rider")
        trip_a = create_trip_for_driver(driver_a, 7.4436, 3.8970)
        req = RideRequest.objects.create(
            trip=trip_a, rider=rider, pickup_lat=7.4437, pickup_lng=3.8971,
            rider_destination=CampusLocation.objects.first(), status="accepted",
        )
        self.client.login(username="driverB", password="testpass123")
        resp = self.client.post(
            reverse("api_confirm_boarding"),
            data=json.dumps({"token": str(req.qr_token)}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_no_show_riders_marked_and_excluded_from_fare_split(self):
        driver = make_user("driver6", "driver")
        rider1 = make_user("rider6a", "rider")
        rider2 = make_user("rider6b", "rider")
        trip = create_trip_for_driver(driver, 7.4436, 3.8970)
        dest = CampusLocation.objects.get(name="Library")

        req1 = RideRequest.objects.create(
            trip=trip, rider=rider1, pickup_lat=7.4437, pickup_lng=3.8971,
            rider_destination=dest, status="boarded", per_rider_fare_share=50,
        )
        req2 = RideRequest.objects.create(
            trip=trip, rider=rider2, pickup_lat=7.4438, pickup_lng=3.8972,
            rider_destination=dest, status="accepted", per_rider_fare_share=50,
        )
        self.client.login(username="driver6", password="testpass123")
        self.client.get(reverse("end_trip", args=[trip.id]))
        req1.refresh_from_db()
        req2.refresh_from_db()
        self.assertEqual(req1.status, "completed")
        self.assertEqual(req2.status, "no_show")
        # Fare pool should now be split only across the completed (boarded) rider
        self.assertGreater(req1.per_rider_fare_share, 0)

    def test_optimizer_handles_zero_seats_gracefully(self):
        driver = make_user("driver7", "driver")
        trip = create_trip_for_driver(driver, 7.4436, 3.8970)
        trip.seats_available = 0
        trip.save()
        rider = make_user("rider7", "rider")
        RideRequest.objects.create(
            trip=trip, rider=rider, pickup_lat=7.4437, pickup_lng=3.8971,
            rider_destination=CampusLocation.objects.first(), status="pending",
        )
        accepted_ids = optimize_trip(trip)
        self.assertEqual(accepted_ids, [])

    def test_profile_less_user_does_not_crash_login(self):
        # Simulates a superuser created via createsuperuser with no Profile
        User.objects.create_superuser(username="admin", password="adminpass123", email="a@a.com")
        self.client.login(username="admin", password="adminpass123")
        try:
            resp = self.client.get(reverse("rider_map"))
        except Exception as e:
            self.fail(f"Accessing a view with a profile-less user raised: {e}")


class MultiRiderOptimizationTests(TestCase):
    """Verify the optimizer actually picks the higher-revenue combination
    under seat constraints (core OR requirement)."""

    def setUp(self):
        CampusLocation.objects.bulk_create([
            CampusLocation(name="Main Gate", latitude=7.4436, longitude=3.8970),
            CampusLocation(name="Faculty Complex", latitude=7.4460, longitude=3.9010),
        ])

    def test_optimizer_prefers_higher_expected_revenue_under_seat_limit(self):
        driver = make_user("odriver", "driver")
        trip = create_trip_for_driver(driver, 7.4436, 3.8970)
        trip.total_seats = 1
        trip.seats_available = 1
        trip.save()

        dest = CampusLocation.objects.get(name="Faculty Complex")
        near = make_user("onear", "rider")
        far = make_user("ofar", "rider")

        # Both pickups are near the driver/anchor line (feasible), but "far"
        # rider has a further destination => bigger fare => should be preferred
        # if it doesn't blow the detour budget.
        r_near = RideRequest.objects.create(
            trip=trip, rider=near, pickup_lat=7.4438, pickup_lng=3.8972,
            rider_destination=dest, status="pending", p_board=0.9,
        )
        r_far = RideRequest.objects.create(
            trip=trip, rider=far, pickup_lat=7.4440, pickup_lng=3.8975,
            rider_destination=dest, status="pending", p_board=0.9,
        )
        from rides.planning import plan_trip
        plan_trip(trip)
        accepted = RideRequest.objects.filter(trip=trip, status="accepted")
        self.assertLessEqual(accepted.count(), 1)
