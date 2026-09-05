from django.urls import path
from . import views

urlpatterns = [
    path("", views.rider_map, name="rider_map"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("geofence-blocked/", views.geofence_blocked, name="geofence_blocked"),
    path("driver/", views.driver_map, name="driver_map"),
    # Destination picker removed — system plans the route
    path("driver/go-online/", views.driver_go_online, name="driver_go_online"),
    path("driver/active-trip/", views.driver_active_trip, name="driver_active_trip"),
    path("driver/requests/", views.rider_requests, name="rider_requests"),
    path("driver/scan/", views.qr_scanner, name="qr_scanner"),
    path("end-trip/<int:trip_id>/", views.end_trip, name="end_trip"),
    path("keke/<int:trip_id>/", views.keke_detail, name="keke_detail"),
    path("request/<int:request_id>/waiting/", views.waiting_confirmation, name="waiting_confirmation"),
    path("request/<int:request_id>/qr/", views.qr_display, name="qr_display"),
    path("trip-completed/<int:trip_id>/", views.trip_completed, name="trip_completed"),
    path("api/geofence-check/", views.geofence_check, name="geofence_check"),
    path("api/active-trips/", views.api_active_trips, name="api_active_trips"),
    path("api/request-seat/<int:trip_id>/", views.api_request_seat, name="api_request_seat"),
    path("api/request-status/<int:request_id>/", views.api_request_status, name="api_request_status"),
    path("api/confirm-boarding/", views.api_confirm_boarding, name="api_confirm_boarding"),
    path("api/cancel-request/<int:request_id>/", views.api_cancel_request, name="api_cancel_request"),
    path("api/update-location/", views.api_update_driver_location, name="api_update_driver_location"),
    path("api/driver-status/", views.api_driver_trip_status, name="api_driver_status"),
    path("api/update-driver-location/", views.api_update_driver_location, name="api_update_driver_location_alias"),
    path("api/driver-trip-status/", views.api_driver_trip_status, name="api_driver_trip_status"),
    path("api/optimize/<int:trip_id>/", views.api_optimize_preview, name="api_optimize_preview"),
    path("api/accept-optimized/<int:trip_id>/", views.api_accept_optimized, name="api_accept_optimized"),
]
