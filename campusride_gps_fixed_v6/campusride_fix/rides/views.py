import json
import base64
import io
import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction
from django.utils import timezone

from .models import Profile, CampusLocation, ActiveTrip, RideRequest
from .geofence import is_within_campus
from .fares import calculate_fare, trip_revenue_pool, shared_shares
from .planning import plan_trip, create_trip_for_driver, enrich_request_scores
from .constants import DEFAULT_TOTAL_SEATS
from .db_utils import retry_on_db_lock
import qrcode

logger = logging.getLogger(__name__)


# ---------- Auth ----------
def _normalize_username(id_number: str, full_name: str) -> str:
    """
    Normalize the login identifier so the same person always maps to the
    same account regardless of how they capitalize their matric/phone number.
    """
    base = id_number.strip() if id_number.strip() else full_name.strip()
    return base.lower().replace(" ", "_")


def login_view(request):
    """
    Real password-based auth.
    - New identifier (name/ID combo) + password -> creates the account with that
      password and logs them in (first-time signup).
    - Existing identifier -> the typed password must match, via Django's
      authenticate(), or the login is rejected. This closes the "type anyone's
      name to become them" hole and prevents unauthorized role flips.
    """
    error = None
    if request.method == "POST":
        full_name = request.POST.get("full_name", "").strip()
        id_number = request.POST.get("id_number", "").strip()
        password = request.POST.get("password", "")
        role = request.POST.get("role", "rider")

        username = _normalize_username(id_number, full_name)

        if not username or not password:
            error = "Please fill in your name/ID and a password."
        else:
            existing_user = User.objects.filter(username=username).first()

            if existing_user is None:
                # First time we've seen this identifier: create the account.
                user = User.objects.create_user(
                    username=username, password=password, first_name=full_name
                )
                Profile.objects.create(
                    user=user,
                    role="driver" if role == "staff" else "rider",
                    phone_or_matric_number=id_number,
                )
                login(request, user)
            else:
                user = authenticate(request, username=username, password=password)
                if user is None:
                    error = "Incorrect password for that name/ID."
                else:
                    # Role is fixed at signup; it can't be changed just by
                    # re-logging in with a different toggle selection.
                    login(request, user)

        if error is None:
            profile = request.user.profile
            if profile.role == "driver":
                return redirect("driver_map")
            return redirect("rider_map")

    return render(request, "rides/login.html", {"error": error})


def logout_view(request):
    logout(request)
    return redirect("login")


@csrf_exempt
def geofence_check(request):
    try:
        data = json.loads(request.body)
        lat, lng = data.get("lat"), data.get("lng")
        if lat is None or lng is None:
            return JsonResponse({"error": "Missing coordinates"}, status=400)
        return JsonResponse({"within_campus": is_within_campus(lat, lng)})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


def geofence_blocked(request):
    return render(request, "rides/geofence_blocked.html")


# ---------- Rider ----------
@login_required
def rider_map(request):
    trips = ActiveTrip.objects.filter(
        status__in=["active", "forming"], seats_available__gt=0
    ).select_related("driver", "destination")
    return render(request, "rides/rider_map.html", {"trips": trips})


@login_required
def keke_detail(request, trip_id):
    trip = get_object_or_404(ActiveTrip, id=trip_id, status__in=["active", "forming", "full"])
    destinations = CampusLocation.objects.all()
    # Display estimate assuming joining this trip
    n = max(1, trip.total_seats - trip.seats_available + 1)
    pool = trip.total_trip_fare_estimate or 0
    fare_info = {
        "total_fare": pool,
        "per_rider_share": shared_shares(pool, n) if pool else 0,
        "expected_revenue": trip.expected_revenue,
    }
    return render(
        request,
        "rides/keke_detail.html",
        {
            "trip": trip,
            "fare": fare_info,
            "destinations": destinations,
            "plan_explanation": trip.plan_explanation,
            "route": trip.planned_route_json or [],
            "seat_range": range(trip.total_seats),
            "occupied_seats": trip.total_seats - trip.seats_available,
        },
    )


@login_required
def waiting_confirmation(request, request_id):
    ride_req = get_object_or_404(
        RideRequest.objects.select_related("trip", "trip__driver", "trip__destination"),
        id=request_id,
        rider=request.user,
    )
    return render(
        request,
        "rides/waiting_confirmation.html",
        {"request_id": ride_req.id, "request": ride_req, "trip": ride_req.trip},
    )


@login_required
def qr_display(request, request_id):
    ride_req = get_object_or_404(RideRequest, id=request_id, rider=request.user)
    qr = qrcode.make(str(ride_req.qr_token))
    buffer = io.BytesIO()
    qr.save(buffer, format="PNG")
    qr_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return render(
        request,
        "rides/qr_display.html",
        {"qr_base64": qr_base64, "request": ride_req, "qr_token": ride_req.qr_token},
    )


# ---------- Driver (no destination picker) ----------
@login_required
def driver_map(request):
    try:
        ActiveTrip.objects.get(
            driver=request.user, status__in=["active", "forming", "full"]
        )
        return redirect("driver_active_trip")
    except ActiveTrip.DoesNotExist:
        return render(request, "rides/driver_map.html", {"has_active": False})


@login_required
@retry_on_db_lock()
def driver_go_online(request):
    """
    Replace set_destination: driver only sends GPS; system plans the trip.
    """
    if request.method != "POST":
        return redirect("driver_map")

    lat = request.POST.get("lat")
    lng = request.POST.get("lng")
    location_source = (request.POST.get("location_source") or "gps").strip().lower()

    # Never silently replace a missing/bad device fix with campus coordinates.
    # The only legitimate fallback is an explicit development/test action.
    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return JsonResponse(
            {"error": "A valid GPS latitude and longitude are required. Retry GPS."},
            status=400,
        ) if request.headers.get("Accept") == "application/json" else render(
            request, "rides/driver_map.html",
            {"error": "A valid GPS location is required. Please retry GPS."},
            status=400,
        )

    if not (-90 <= lat_f <= 90 and -180 <= lng_f <= 180):
        return JsonResponse(
            {"error": "GPS coordinates are outside valid geographic bounds."}, status=400
        ) if request.headers.get("Accept") == "application/json" else render(
            request, "rides/driver_map.html",
            {"error": "Invalid GPS coordinates. Please retry GPS."}, status=400
        )

    if location_source not in {"gps", "test"}:
        return JsonResponse({"error": "Invalid location source."}, status=400)

    # Ensure at least a few campus stops exist for anchors / rider dropdown
    if CampusLocation.objects.count() == 0:
        CampusLocation.objects.bulk_create(
            [
                CampusLocation(name="Main Gate", latitude=7.4436, longitude=3.8970),
                CampusLocation(name="Library", latitude=7.4450, longitude=3.8990),
                CampusLocation(name="Hostel Area", latitude=7.4410, longitude=3.8950),
                CampusLocation(name="Faculty Complex", latitude=7.4460, longitude=3.9010),
                CampusLocation(name="Sports Complex", latitude=7.4400, longitude=3.8930),
            ]
        )

    # Serialize the "no active trip → create" check under a lock so two
    # rapid taps of Go Online cannot both observe zero active trips and
    # both create one.
    with transaction.atomic():
        existing = (
            ActiveTrip.objects.select_for_update()
            .filter(driver=request.user, status__in=["active", "forming", "full"])
            .first()
        )
        if existing:
            return redirect("driver_active_trip")
        create_trip_for_driver(request.user, lat_f, lng_f)
    return redirect("driver_active_trip")


@login_required
def driver_active_trip(request):
    try:
        trip = ActiveTrip.objects.get(
            driver=request.user, status__in=["active", "forming", "full"]
        )
    except ActiveTrip.DoesNotExist:
        return redirect("driver_map")
    pending_count = trip.requests.filter(status="pending").count()
    accepted = trip.requests.filter(status__in=["accepted", "boarded"]).select_related(
        "rider", "rider_destination"
    )
    return render(
        request,
        "rides/driver_active_trip.html",
        {
            "trip": trip,
            "pending_count": pending_count,
            "accepted": accepted,
            "route": trip.planned_route_json or [],
            "plan_explanation": trip.plan_explanation,
        },
    )


@login_required
@retry_on_db_lock()
def rider_requests(request):
    """System-led: mostly read-only; optional manual override still available."""
    trip = get_object_or_404(
        ActiveTrip, driver=request.user, status__in=["active", "forming", "full"]
    )
    if request.method == "POST":
        req_id = request.POST.get("request_id")
        action = request.POST.get("action")
        ride_req = get_object_or_404(RideRequest, id=req_id, trip=trip, status="pending")
        if action == "accept":
            # Lock the trip row and re-check seats_available and the
            # request's own status *after* acquiring the lock -- both were
            # read above outside any lock and could be stale by the time we
            # get here if another accept/cancel/optimize ran concurrently.
            with transaction.atomic():
                locked_trip = ActiveTrip.objects.select_for_update().get(pk=trip.pk)
                if locked_trip.seats_available <= 0:
                    return JsonResponse({"error": "No seats left"}, status=400)
                locked_req = RideRequest.objects.select_for_update().filter(
                    id=req_id, trip=locked_trip, status="pending"
                ).first()
                if locked_req is None:
                    return JsonResponse({"error": "Request no longer pending"}, status=400)
                locked_req.status = "accepted"
                locked_req.save(update_fields=["status"])
                locked_trip.seats_available -= 1
                if locked_trip.seats_available <= 0:
                    locked_trip.status = "full"
                locked_trip.save(update_fields=["seats_available", "status"])
            plan_trip(locked_trip)
            return JsonResponse({"success": True})
        if action == "decline":
            ride_req.status = "declined"
            ride_req.save(update_fields=["status"])
            return JsonResponse({"success": True})
        if action == "replan":
            plan_trip(trip)
            return JsonResponse({"success": True})
        return JsonResponse({"error": "Invalid action"}, status=400)

    pending = trip.requests.filter(status="pending").order_by("-p_board", "-compatibility_score")
    accepted = trip.requests.filter(status__in=["accepted", "boarded"])
    return render(
        request,
        "rides/rider_requests.html",
        {"trip": trip, "pending": pending, "accepted": accepted},
    )


@login_required
def qr_scanner(request):
    trip = ActiveTrip.objects.filter(
        driver=request.user, status__in=["active", "forming", "full"]
    ).first()
    context = {"trip": trip}
    if trip is not None:
        context["queue"] = trip.requests.filter(
            status__in=["accepted", "boarded"]
        ).select_related("rider", "rider_destination")
    return render(request, "rides/qr_scanner.html", context)


@login_required
@retry_on_db_lock()
def end_trip(request, trip_id):
    # The get_object_or_404 status filter alone isn't enough to prevent a
    # double-tap/double-submit of "End Trip" from running this whole block
    # twice concurrently -- both calls could pass the filter before either
    # writes status="completed". Re-check status again after acquiring the
    # row lock inside the atomic block below.
    trip = get_object_or_404(
        ActiveTrip, id=trip_id, driver=request.user, status__in=["active", "forming", "full"]
    )
    with transaction.atomic():
        trip = ActiveTrip.objects.select_for_update().get(pk=trip.pk)
        if trip.status == "completed":
            return redirect("trip_completed", trip_id=trip.id)
        trip.status = "completed"
        trip.completed_at = timezone.now()
        trip.save(update_fields=["status", "completed_at"])
        trip.requests.filter(status="boarded").update(status="completed")
        # accepted but not boarded → no_show
        trip.requests.filter(status="accepted").update(status="no_show")

        # Re-normalize fare shares across only the riders who actually
        # completed the trip, so no-shows don't silently shrink the
        # revenue reported to the driver. The original per-accept split
        # (pool / all_accepted) is no longer valid once some riders never
        # boarded.
        completed_reqs = list(trip.requests.filter(status="completed").select_related("rider_destination"))
        pool = trip_revenue_pool(completed_reqs)
        share = shared_shares(pool, len(completed_reqs)) if completed_reqs else 0.0
        for req in completed_reqs:
            req.per_rider_fare_share = share
        RideRequest.objects.bulk_update(completed_reqs, ["per_rider_fare_share"])
        trip.total_trip_fare_estimate = pool
        trip.save(update_fields=["total_trip_fare_estimate"])

        # Persist a TripHistory snapshot so completed trips remain queryable
        # after ActiveTrip rows age out or are archived. Decision (Task 6):
        # wire it here rather than leave the model unused.
        from .models import TripHistory
        duration = None
        if trip.created_at and trip.completed_at:
            duration = int((trip.completed_at - trip.created_at).total_seconds())
        TripHistory.objects.update_or_create(
            trip=trip,
            defaults={
                "total_riders": len(completed_reqs),
                "fuel_cost_estimate": None,
                "fuel_cost_per_passenger": None,
                "total_trip_duration_seconds": duration,
            },
        )

    return redirect("trip_completed", trip_id=trip.id)


@login_required
def trip_completed(request, trip_id):
    trip = get_object_or_404(ActiveTrip, id=trip_id, driver=request.user)
    completed = trip.requests.filter(status="completed")
    completed_count = completed.count()
    total_fare = sum(req.per_rider_fare_share for req in completed)
    per_rider = completed.first().per_rider_fare_share if completed.exists() else 0
    return render(
        request,
        "rides/trip_completed.html",
        {
            "trip": trip,
            "total_fare": total_fare,
            "per_rider_fare": per_rider,
            "completed_count": completed_count,
            "completed_requests": completed,
            "expected_revenue": trip.expected_revenue,
            "plan_explanation": trip.plan_explanation,
        },
    )


@login_required
@csrf_exempt
@retry_on_db_lock()
def api_cancel_request(request, request_id):
    """Let a rider cancel their own pending/accepted seat request."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    ride_req = get_object_or_404(RideRequest, id=request_id, rider=request.user)
    if ride_req.status not in ("pending", "accepted"):
        return JsonResponse({"error": "Request can no longer be cancelled"}, status=400)

    trip_id = ride_req.trip_id
    with transaction.atomic():
        # Re-fetch and lock both rows: a concurrent accept/optimize could
        # have changed ride_req.status or trip.seats_available between the
        # unlocked read above and here.
        ride_req = RideRequest.objects.select_for_update().get(pk=ride_req.pk)
        if ride_req.status not in ("pending", "accepted"):
            return JsonResponse({"error": "Request can no longer be cancelled"}, status=400)
        trip = ActiveTrip.objects.select_for_update().get(pk=trip_id)
        was_accepted = ride_req.status == "accepted"
        ride_req.status = "declined"
        ride_req.save(update_fields=["status"])
        if was_accepted:
            trip.seats_available = min(trip.total_seats, trip.seats_available + 1)
            if trip.status == "full" and trip.seats_available > 0:
                trip.status = "active"
            trip.save(update_fields=["seats_available", "status"])
    # Re-run the optimizer so a freed seat can be offered to other pending riders.
    plan_trip(trip)
    return JsonResponse({"success": True})


# ---------- APIs ----------
@login_required
def api_active_trips(request):
    trips = ActiveTrip.objects.filter(
        status__in=["active", "forming"], seats_available__gt=0
    ).select_related("destination", "driver")
    data = []
    for trip in trips:
        lat = trip.driver_lat
        lng = trip.driver_lng
        # Never send null/invalid coords — rider map would drop the marker
        if lat is None or lng is None:
            continue
        data.append(
            {
                "id": trip.id,
                "driver": trip.driver.username,
                "destination": trip.destination.name if trip.destination_id else "System plan",
                "lat": float(lat),
                "lng": float(lng),
                "seats_available": trip.seats_available,
                "total_seats": trip.total_seats,
                "fare_estimate": trip.total_trip_fare_estimate or 0,
                "expected_revenue": trip.expected_revenue or 0,
                "plan": (trip.plan_explanation or "")[:120],
            }
        )
    return JsonResponse({"trips": data})


@login_required
@retry_on_db_lock(max_attempts=8, base_delay=0.08)
def api_request_seat(request, trip_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    try:
        data = json.loads(request.body)
        pickup_lat = data.get("lat")
        pickup_lng = data.get("lng")
        rider_dest_id = data.get("destination_id")
    except Exception:
        return JsonResponse({"error": "Invalid request body"}, status=400)
    if pickup_lat is None or pickup_lng is None or not rider_dest_id:
        return JsonResponse({"error": "Missing location or destination stop"}, status=400)

    rider_dest = get_object_or_404(CampusLocation, id=rider_dest_id)

    # Create under lock so two concurrent last-seat requests cannot both
    # observe seats_available > 0 and both insert a pending row that the
    # subsequent plan_trip then accepts. Re-read seats after the lock.
    with transaction.atomic():
        trip = ActiveTrip.objects.select_for_update().filter(
            id=trip_id, status__in=["active", "forming", "full"]
        ).first()
        if trip is None:
            return JsonResponse({"error": "Trip not found"}, status=404)
        if trip.seats_available <= 0:
            return JsonResponse({"error": "Trip is full"}, status=400)
        ride_req = RideRequest.objects.create(
            trip=trip,
            rider=request.user,
            pickup_lat=float(pickup_lat),
            pickup_lng=float(pickup_lng),
            rider_destination=rider_dest,
            status="pending",
            compatibility_score=0,
            p_board=0.5,
            per_rider_fare_share=0,
        )

    # Matching + ML score this single request (uses the same anchor logic as
    # the rest of the pipeline — see planning._anchor_coords — so we don't
    # duplicate/diverge on how the anchor fallback is computed).
    is_feasible = enrich_request_scores(trip, ride_req)
    if not is_feasible:
        ride_req.status = "declined"
        ride_req.save(update_fields=["status"])
        return JsonResponse(
            {
                "error": "Not feasible on current system plan",
                "request_id": ride_req.id,
                "status": "declined",
            },
            status=400,
        )

    plan_trip(trip)
    ride_req.refresh_from_db()
    return JsonResponse(
        {
            "request_id": ride_req.id,
            "status": ride_req.status,
            "p_board": ride_req.p_board,
            "compatibility_score": ride_req.compatibility_score,
        }
    )


@login_required
def api_request_status(request, request_id):
    ride_req = get_object_or_404(RideRequest, id=request_id, rider=request.user)
    return JsonResponse(
        {
            "status": ride_req.status,
            "qr_token": str(ride_req.qr_token) if ride_req.status == "accepted" else None,
            "trip_id": ride_req.trip_id,
            "per_rider_fare_share": ride_req.per_rider_fare_share,
        }
    )


@csrf_exempt
@login_required
@retry_on_db_lock()
def api_confirm_boarding(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    try:
        data = json.loads(request.body)
        token = data.get("token") or data.get("request_id")
        if not token:
            return JsonResponse({"error": "Missing token"}, status=400)
        # Two rapid scans of the same QR (double-tap, retried request, or a
        # second driver device) must not both succeed. Locking the row and
        # re-checking status="accepted" *after* acquiring the lock closes
        # that race: whichever scan gets the lock first flips the status to
        # "boarded", and the second scan's own select_for_update() query
        # then finds nothing matching status="accepted" and 404s cleanly
        # instead of both scans reading "accepted" and both writing
        # "boarded".
        with transaction.atomic():
            try:
                ride_req = RideRequest.objects.select_for_update().get(
                    qr_token=token, status="accepted"
                )
            except (RideRequest.DoesNotExist, ValueError):
                ride_req = get_object_or_404(
                    RideRequest.objects.select_for_update(), id=token, status="accepted"
                )
            if ride_req.trip.driver != request.user:
                return JsonResponse({"error": "Not your trip"}, status=403)
            ride_req.status = "boarded"
            ride_req.save(update_fields=["status"])
        return JsonResponse({"success": True})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@login_required
@csrf_exempt
def api_update_driver_location(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    try:
        trip = ActiveTrip.objects.get(
            driver=request.user, status__in=["active", "forming", "full"]
        )
    except ActiveTrip.DoesNotExist:
        return JsonResponse({"error": "No active trip"}, status=404)
    try:
        data = json.loads(request.body)
        lat, lng = data.get("lat"), data.get("lng")
        if lat is None or lng is None:
            return JsonResponse({"error": "Missing lat/lng"}, status=400)
        lat_f, lng_f = float(lat), float(lng)
        if not (-90 <= lat_f <= 90 and -180 <= lng_f <= 180):
            return JsonResponse({"error": "Coordinates outside valid geographic bounds"}, status=400)
        trip.driver_lat = lat_f
        trip.driver_lng = lng_f
        trip.save(update_fields=["driver_lat", "driver_lng"])
        return JsonResponse({"success": True, "lat": trip.driver_lat, "lng": trip.driver_lng})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@login_required
def api_driver_trip_status(request):
    try:
        trip = ActiveTrip.objects.get(
            driver=request.user, status__in=["active", "forming", "full"]
        )
    except ActiveTrip.DoesNotExist:
        return JsonResponse({"error": "No active trip"}, status=404)
    pending_count = trip.requests.filter(status="pending").count()
    accepted_count = trip.requests.filter(status__in=["accepted", "boarded"]).count()
    return JsonResponse(
        {
            "trip_id": trip.id,
            "status": trip.status,
            "seats_available": trip.seats_available,
            "total_seats": trip.total_seats,
            "pending_count": pending_count,
            "accepted_count": accepted_count,
            "driver_lat": trip.driver_lat,
            "driver_lng": trip.driver_lng,
            "destination": trip.destination.name if trip.destination_id else "System plan",
            "expected_revenue": trip.expected_revenue,
            "total_trip_fare_estimate": trip.total_trip_fare_estimate,
            "plan_explanation": trip.plan_explanation,
            "route": trip.planned_route_json or [],
        }
    )


@login_required
def api_optimize_preview(request, trip_id):
    """
    Non-mutating preview of which pending requests the optimizer would pick
    to maximize expected revenue, for the driver's "Optimize (AI)" button.
    """
    trip = get_object_or_404(ActiveTrip, id=trip_id, driver=request.user)
    from .optimizer import _milp_select, _greedy_select
    from .fares import expected_revenue

    pending = list(
        trip.requests.filter(status="pending").select_related("rider_destination", "rider")
    )
    seats = max(0, trip.seats_available)
    selected = _milp_select(pending, seats)
    if selected is None:
        selected = _greedy_select(pending, seats)

    recommended_ids = [r.id for r in selected]
    details = [
        {
            "id": r.id,
            "p_show": r.p_board,
            "compatibility_score": r.compatibility_score,
        }
        for r in selected
    ]
    total_expected_profit = expected_revenue(selected, use_p_board=True)
    return JsonResponse(
        {
            "recommended_ids": recommended_ids,
            "details": details,
            "total_expected_profit": total_expected_profit,
        }
    )


@login_required
@csrf_exempt
@retry_on_db_lock()
def api_accept_optimized(request, trip_id):
    """Accept the set of pending requests the preview recommended."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    trip = get_object_or_404(ActiveTrip, id=trip_id, driver=request.user)
    try:
        data = json.loads(request.body)
        recommended_ids = data.get("recommended_ids") or []
    except Exception:
        return JsonResponse({"error": "Invalid request body"}, status=400)

    if not recommended_ids:
        return JsonResponse({"error": "No recommended requests provided"}, status=400)

    # Seat count and the pending-status filter are both re-read *after*
    # acquiring the trip row lock, not from the `trip`/`recommended_ids`
    # read above -- otherwise a concurrent seat request, cancel, or another
    # accept-optimized call could change seats_available or a request's
    # status between that unlocked read and this write.
    with transaction.atomic():
        trip = ActiveTrip.objects.select_for_update().get(pk=trip.pk)
        seats = max(0, trip.seats_available)
        to_accept = list(
            RideRequest.objects.select_for_update()
            .filter(trip=trip, id__in=recommended_ids, status="pending")
        )[:seats]

        for ride_req in to_accept:
            ride_req.status = "accepted"
            ride_req.save(update_fields=["status"])
        num = len(to_accept)
        if num > 0:
            trip.seats_available = max(0, trip.seats_available - num)
            if trip.seats_available <= 0:
                trip.status = "full"
            trip.save(update_fields=["seats_available", "status"])

    # Recompute shared fare shares/route/expected revenue consistently.
    plan_trip(trip)
    return JsonResponse({"success": True, "accepted": len(to_accept)})


# aliases used by some templates
api_driver_status = api_driver_trip_status
