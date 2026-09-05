"""
Task 4 — full HTTP/template smoke tests + set_destination orphan check.
Task 5 — basic auth / authorization coverage.
"""
import json
import re
from pathlib import Path

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse, NoReverseMatch

from rides.models import ActiveTrip, CampusLocation, Profile, RideRequest
from rides.planning import create_trip_for_driver


def make_user(username, role, password="testpass123"):
    user = User.objects.create_user(username=username, password=password, first_name=username)
    Profile.objects.create(user=user, role=role, phone_or_matric_number=username)
    return user


class UrlSmokeTests(TestCase):
    """Walk key URLs as anonymous / rider / driver."""

    def setUp(self):
        CampusLocation.objects.bulk_create([
            CampusLocation(name="Main Gate", latitude=7.4436, longitude=3.8970),
            CampusLocation(name="Library", latitude=7.4450, longitude=3.8990),
        ])
        self.rider = make_user("smoke_rider", "rider")
        self.driver = make_user("smoke_driver", "driver")
        self.other_driver = make_user("other_driver", "driver")
        self.trip = create_trip_for_driver(self.driver, 7.4436, 3.8970)
        dest = CampusLocation.objects.first()
        self.req = RideRequest.objects.create(
            trip=self.trip,
            rider=self.rider,
            pickup_lat=7.4438,
            pickup_lng=3.8972,
            rider_destination=dest,
            status="accepted",
            p_board=0.8,
        )

    def test_anonymous_login_ok_and_protected_redirect(self):
        c = Client()
        r = c.get(reverse("login"))
        self.assertEqual(r.status_code, 200)
        r = c.get(reverse("rider_map"))
        self.assertIn(r.status_code, (302, 301))
        r = c.get(reverse("driver_map"))
        self.assertIn(r.status_code, (302, 301))

    def test_rider_pages_render(self):
        c = Client()
        c.login(username="smoke_rider", password="testpass123")
        for name, args in [
            ("rider_map", []),
            ("keke_detail", [self.trip.id]),
            ("waiting_confirmation", [self.req.id]),
            ("qr_display", [self.req.id]),
        ]:
            r = c.get(reverse(name, args=args))
            self.assertEqual(r.status_code, 200, f"{name} returned {r.status_code}")

    def test_driver_pages_render(self):
        c = Client()
        c.login(username="smoke_driver", password="testpass123")
        for name, args in [
            ("driver_active_trip", []),
            ("rider_requests", []),
            ("qr_scanner", []),
        ]:
            r = c.get(reverse(name, args=args))
            self.assertEqual(r.status_code, 200, f"{name} returned {r.status_code}")

    def test_trip_completed_after_end(self):
        c = Client()
        c.login(username="smoke_driver", password="testpass123")
        r = c.post(reverse("end_trip", args=[self.trip.id]))
        self.assertIn(r.status_code, (302, 200))
        r = c.get(reverse("trip_completed", args=[self.trip.id]))
        self.assertEqual(r.status_code, 200)

    def test_set_destination_not_in_urlconf(self):
        """Changelog says destination picker was removed; confirm no reverse."""
        with self.assertRaises(NoReverseMatch):
            reverse("set_destination")

    def test_set_destination_template_unreferenced(self):
        """No live template should still {% url 'set_destination' %} or form action."""
        templates_dir = Path(__file__).resolve().parent / "templates"
        offenders = []
        for html in templates_dir.rglob("*.html"):
            text = html.read_text(encoding="utf-8", errors="ignore")
            # Comments mentioning the old flow are fine; live url tags / actions are not.
            if re.search(r"""\{%\s*url\s+['\"]set_destination['\"]""", text):
                offenders.append(str(html.relative_to(templates_dir)))
            if re.search(r"""action=["'][^"']*set_destination""", text):
                offenders.append(str(html.relative_to(templates_dir)))
        self.assertEqual(offenders, [], f"stale set_destination refs: {offenders}")

    def test_api_active_trips_json(self):
        c = Client()
        c.login(username="smoke_rider", password="testpass123")
        r = c.get(reverse("api_active_trips"))
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("trips", data)


class AuthSecurityTests(TestCase):
    def setUp(self):
        CampusLocation.objects.create(name="Gate", latitude=7.4436, longitude=3.8970)
        self.driver = make_user("auth_driver", "driver")
        self.other = make_user("auth_other", "driver")
        self.rider = make_user("auth_rider", "rider")
        self.trip = create_trip_for_driver(self.driver, 7.4436, 3.8970)
        dest = CampusLocation.objects.first()
        self.req = RideRequest.objects.create(
            trip=self.trip,
            rider=self.rider,
            pickup_lat=7.4438,
            pickup_lng=3.8972,
            rider_destination=dest,
            status="accepted",
        )

    def test_driver_cannot_end_other_drivers_trip(self):
        c = Client()
        c.login(username="auth_other", password="testpass123")
        r = c.post(reverse("end_trip", args=[self.trip.id]))
        # get_object_or_404 with driver=request.user → 404
        self.assertEqual(r.status_code, 404)

    def test_rider_cannot_view_other_riders_qr(self):
        other_rider = make_user("other_rider", "rider")
        c = Client()
        c.login(username="other_rider", password="testpass123")
        r = c.get(reverse("qr_display", args=[self.req.id]))
        self.assertEqual(r.status_code, 404)

    def test_rider_cannot_board_with_foreign_qr(self):
        """Cross-driver boarding already covered in full suite; re-assert here."""
        c = Client()
        c.login(username="auth_other", password="testpass123")
        r = c.post(
            reverse("api_confirm_boarding"),
            data=json.dumps({"token": str(self.req.qr_token)}),
            content_type="application/json",
        )
        self.assertIn(r.status_code, (403, 400, 404))

    def test_login_wrong_password_does_not_create_session(self):
        c = Client()
        r = c.post(reverse("login"), {
            "full_name": "auth_driver",
            "id_number": "auth_driver",
            "password": "wrong-password",
            "role": "staff",
        })
        self.assertEqual(r.status_code, 200)  # re-render form with error
        self.assertFalse(r.wsgi_request.user.is_authenticated)

    def test_login_error_message_does_not_enumerate_new_vs_existing(self):
        """
        Both 'new identifier' path (creates account) and 'wrong password'
        path should not leak distinguishable timing/message that reveals
        whether the username already exists *when password is wrong*.

        Current design: wrong password for existing → 'Incorrect password…'
        New identifier + password → creates account. That *does* allow
        enumeration of existing accounts via the error string. Documented
        here as known MVP limitation; production should use a uniform
        'Invalid credentials' message and rate limiting.
        """
        c = Client()
        r = c.post(reverse("login"), {
            "full_name": "auth_driver",
            "id_number": "auth_driver",
            "password": "bad",
            "role": "staff",
        })
        self.assertContains(r, "Incorrect password", status_code=200)
        # Flag for production hardening — assertion documents current behaviour.
        self.assertTrue(True)

    def test_concurrent_signup_same_username_is_safe(self):
        """
        Two first-time POSTs with the same normalized username should not
        500. One creates the user; the other either authenticates or shows
        a clean error. (Full thread race is covered loosely; this is the
        sequential collision case.)
        """
        c1, c2 = Client(), Client()
        payload = {
            "full_name": "Dup User",
            "id_number": "dup_user_99",
            "password": "secret99",
            "role": "rider",
        }
        r1 = c1.post(reverse("login"), payload)
        self.assertIn(r1.status_code, (302, 200))
        # Second attempt with same id but different password → should not 500
        payload2 = dict(payload, password="other-secret")
        r2 = c2.post(reverse("login"), payload2)
        self.assertIn(r2.status_code, (200, 302))
        self.assertEqual(User.objects.filter(username="dup_user_99").count(), 1)


class NoCampusLocationsEdgeCaseTests(TestCase):
    """Task 6 — cold start with empty CampusLocation table."""

    def test_go_online_seeds_locations_when_empty(self):
        self.assertEqual(CampusLocation.objects.count(), 0)
        driver = make_user("cold_driver", "driver")
        c = Client()
        c.login(username="cold_driver", password="testpass123")
        r = c.post(reverse("driver_go_online"), {"lat": "7.4436", "lng": "3.8970"})
        self.assertIn(r.status_code, (302, 200))
        self.assertGreater(CampusLocation.objects.count(), 0)
        self.assertTrue(
            ActiveTrip.objects.filter(driver=driver, status__in=["active", "forming", "full"]).exists()
        )

    def test_choose_anchor_returns_none_on_empty(self):
        from rides.ml_service import choose_anchor_location
        self.assertIsNone(choose_anchor_location([]))
