"""
Concurrency / race-condition tests for the seat-request and optimize flow.

These use TransactionTestCase (not TestCase) because TestCase wraps each
test in a single non-committed transaction, which means threads other than
the test's own thread can't see rows the test thread has written -- that
would hide the exact race we're testing for. TransactionTestCase commits for
real and truncates tables between tests instead, so concurrent threads see
real committed state, same as they would in production.

Run with: python manage.py test rides.test_concurrency -v 2
"""
import json
import threading
import time

from django.contrib.auth.models import User
from django.test import Client, TransactionTestCase
from django.urls import reverse

from rides.models import ActiveTrip, CampusLocation, Profile, RideRequest
from rides.planning import create_trip_for_driver


def make_user(username, role, password="testpass123"):
    user = User.objects.create_user(username=username, password=password)
    Profile.objects.create(user=user, role=role, phone_or_matric_number="000")
    return user


class SeatRequestRaceTests(TransactionTestCase):
    """Two riders request the last seat on a trip at ~the same time."""

    def setUp(self):
        CampusLocation.objects.bulk_create([
            CampusLocation(name="Main Gate", latitude=7.4436, longitude=3.8970),
            CampusLocation(name="Faculty Complex", latitude=7.4460, longitude=3.9010),
        ])

    def _request_seat(self, client, trip_id, pickup_lat, pickup_lng, dest_id, results, barrier, username):
        """Runs in its own thread with its own DB connection (Django gives
        each thread its own connection automatically). `client` must already
        be logged in before the thread starts -- login() does its own writes
        (django_session) and racing those against the other thread's login
        would test session-table contention, not the seat-request race we
        actually care about."""
        barrier.wait()  # line both threads up to fire as close together as possible
        resp = client.post(
            reverse("api_request_seat", args=[trip_id]),
            data=json.dumps({"lat": pickup_lat, "lng": pickup_lng, "destination_id": dest_id}),
            content_type="application/json",
        )
        results[username] = (resp.status_code, resp.json() if resp.status_code == 200 else resp.content)

    def test_two_simultaneous_requests_for_last_seat_only_one_accepted(self):
        driver = make_user("racedriver", "driver")
        rider_a = make_user("racerider_a", "rider")
        rider_b = make_user("racerider_b", "rider")

        trip = create_trip_for_driver(driver, 7.4436, 3.8970)
        trip.refresh_from_db()
        anchor = trip.destination
        # Force down to exactly 1 seat left, regardless of what go-online set.
        trip.total_seats = 1
        trip.seats_available = 1
        trip.status = "active"
        trip.save()

        pickup_lat = trip.driver_lat + (anchor.latitude - trip.driver_lat) * 0.1
        pickup_lng = trip.driver_lng + (anchor.longitude - trip.driver_lng) * 0.1

        # Log in both clients up front, outside the timed race window.
        client_a = Client()
        client_a.login(username="racerider_a", password="testpass123")
        client_b = Client()
        client_b.login(username="racerider_b", password="testpass123")

        results = {}
        barrier = threading.Barrier(2)
        t1 = threading.Thread(
            target=self._request_seat,
            args=(client_a, trip.id, pickup_lat, pickup_lng, anchor.id, results, barrier, "racerider_a"),
        )
        t2 = threading.Thread(
            target=self._request_seat,
            args=(client_b, trip.id, pickup_lat, pickup_lng, anchor.id, results, barrier, "racerider_b"),
        )
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertIn("racerider_a", results)
        self.assertIn("racerider_b", results)

        trip.refresh_from_db()
        accepted_count = RideRequest.objects.filter(trip=trip, status="accepted").count()

        # The core assertion: capacity must never be oversold. With
        # total_seats=1, at most 1 request may end up "accepted" no matter how
        # the two threads interleaved.
        self.assertLessEqual(
            accepted_count, 1,
            f"Oversold seats: {accepted_count} requests accepted on a 1-seat trip "
            f"(results={results}, trip.seats_available={trip.seats_available})",
        )
        # seats_available must never go negative either.
        self.assertGreaterEqual(trip.seats_available, 0)


class BoardingRaceTests(TransactionTestCase):
    """Two near-simultaneous boarding scans of the same QR token."""

    def setUp(self):
        CampusLocation.objects.bulk_create([
            CampusLocation(name="Main Gate", latitude=7.4436, longitude=3.8970),
        ])

    def _confirm_boarding(self, client, token, results, barrier):
        """`client` must already be logged in before the thread starts, same
        reasoning as SeatRequestRaceTests._request_seat -- we want to race
        the boarding-scan endpoint itself, not the login/session write."""
        barrier.wait()
        resp = client.post(
            reverse("api_confirm_boarding"),
            data=json.dumps({"token": str(token)}),
            content_type="application/json",
        )
        results[threading.current_thread().name] = (resp.status_code, resp.content)

    def test_double_scan_only_boards_once(self):
        driver = make_user("boarddriver", "driver")
        rider = make_user("boardrider", "rider")
        trip = create_trip_for_driver(driver, 7.4436, 3.8970)
        dest = CampusLocation.objects.first()
        req = RideRequest.objects.create(
            trip=trip, rider=rider, pickup_lat=7.4437, pickup_lng=3.8971,
            rider_destination=dest, status="accepted",
        )

        # One driver, scanning the same QR twice in quick succession (e.g. a
        # double-tap or a retried request) -- each scan uses its own Client
        # instance (Django's test Client is not thread-safe to share) but
        # logs in ahead of time so login() itself isn't part of the race.
        clients = []
        for _ in range(2):
            c = Client()
            c.login(username="boarddriver", password="testpass123")
            clients.append(c)

        results = {}
        barrier = threading.Barrier(2)
        threads = [
            threading.Thread(
                name=f"scan-{i}",
                target=self._confirm_boarding,
                args=(clients[i], req.qr_token, results, barrier),
            )
            for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        success_count = sum(1 for status, _ in results.values() if status == 200)
        req.refresh_from_db()
        self.assertEqual(req.status, "boarded")
        # Only one scan should have found the request in "accepted" state and
        # transitioned it; a second scan hitting an already-"boarded" request
        # should fail its get_object_or_404(..., status="accepted") lookup.
        self.assertEqual(
            success_count, 1,
            f"Expected exactly one successful boarding scan, got {success_count} (results={results})",
        )
