import uuid
from django.db import models
from django.contrib.auth.models import User


class Profile(models.Model):
    ROLE_CHOICES = [
        ("rider", "Rider"),
        ("driver", "Driver"),
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    phone_or_matric_number = models.CharField(max_length=30)
    keke_association_id = models.CharField(max_length=50, blank=True, null=True)
    vehicle_id = models.CharField(max_length=50, blank=True, null=True)

    def __str__(self):
        return f"{self.user.username} ({self.role})"


class CampusLocation(models.Model):
    name = models.CharField(max_length=100)
    latitude = models.FloatField()
    longitude = models.FloatField()

    def __str__(self):
        return self.name


class ActiveTrip(models.Model):
    STATUS_CHOICES = [
        ("forming", "Forming"),
        ("active", "Active"),
        ("full", "Full"),
        ("completed", "Completed"),
    ]
    driver = models.ForeignKey(User, on_delete=models.CASCADE, related_name="trips")
    driver_lat = models.FloatField()
    driver_lng = models.FloatField()
    # System-chosen anchor (NOT driver-picked destination). Nullable for go-online.
    destination = models.ForeignKey(
        CampusLocation, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="trips_as_anchor",
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="forming")
    total_seats = models.IntegerField(default=3)
    seats_available = models.IntegerField(default=3)
    total_trip_fare_estimate = models.FloatField(default=0)
    expected_revenue = models.FloatField(default=0)
    # Ordered stops: [{lat, lng, type, request_id?, name?}, ...]
    planned_route_json = models.JSONField(default=list, blank=True)
    plan_explanation = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        anchor = self.destination.name if self.destination_id else "no-anchor"
        return f"Trip {self.id} — {self.driver.username} ({anchor})"


class RideRequest(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("accepted", "Accepted"),
        ("boarded", "Boarded"),
        ("completed", "Completed"),
        ("declined", "Declined"),
        ("no_show", "No Show"),
    ]
    trip = models.ForeignKey(ActiveTrip, on_delete=models.CASCADE, related_name="requests")
    rider = models.ForeignKey(User, on_delete=models.CASCADE, related_name="ride_requests")
    pickup_lat = models.FloatField()
    pickup_lng = models.FloatField()
    # Fixed campus stop intent (not free-form route design)
    rider_destination = models.ForeignKey(CampusLocation, on_delete=models.CASCADE)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    compatibility_score = models.FloatField(default=0)
    detour_meters = models.FloatField(default=0)
    p_board = models.FloatField(default=0.5)
    qr_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    per_rider_fare_share = models.FloatField(default=0)
    requested_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Request {self.id} — {self.rider.username} on Trip {self.trip_id}"


class TripHistory(models.Model):
    trip = models.OneToOneField(ActiveTrip, on_delete=models.CASCADE)
    total_riders = models.IntegerField()
    fuel_cost_estimate = models.FloatField(null=True, blank=True)
    fuel_cost_per_passenger = models.FloatField(null=True, blank=True)
    total_trip_duration_seconds = models.IntegerField(null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)
