# CampusRide OR MVP — what changed

## Fixed
- GPS timeout pattern (lenient getCurrentPosition) on rider map / set flows where applicable
- Optimizer objective was minimize-detour (often accepts nobody) → **maximize expected revenue**
- Matching / optimize used different detour ideas → shared `constants.py`
- `optimize_trip` only via CLI → called online from `plan_trip` / go-online / seat request
- ML pickles never loaded → `ml_service.py` with fallbacks
- Fare layer disconnected from optimization → `trip_revenue_pool` + `expected_revenue` + shared shares
- Driver destination required → removed from product flow

## Added
- `rides/constants.py` — shared thresholds
- `rides/ml_service.py` — calibration, p_board, demand, anchor choice
- `rides/planning.py` — `plan_trip`, `create_trip_for_driver`, score enrichment
- `rides/views.driver_go_online` — GPS only, system plans
- Model fields: `expected_revenue`, `planned_route_json`, `plan_explanation`, `p_board`, `detour_meters`, nullable destination, status `forming`
- Migration `0002_or_mvp_plan_fields.py`
- Driver map “Go online” UI
- Plan banner on active trip template
- Requirements: numpy, scikit-learn, pyomo (optional solver)

## Changed
- `rides/optimizer.py` — revenue max, greedy + optional MILP, route JSON, fare shares
- `rides/matching.py` — `evaluate_rider`, marginal detour, shared constants
- `rides/fares.py` — pool, shared shares, expected revenue, calibration-aware ₦/km
- `rides/models.py` — plan-centric ActiveTrip / RideRequest
- `rides/views.py` — full pipeline wiring; no set_destination
- `rides/urls.py` — `driver/go-online/`; destination route removed
- `optimize_all` management command → calls `plan_trip`
- README product description

## Untouched (intentionally)
- Login / logout / role split (Student rider, Staff driver)
- QR display + scanner boarding flow
- Waiting confirmation polling pattern
- Leaflet usage on maps
- Geofence still bypassed for testing (`is_within_campus` → True)
- `TripHistory` model (not auto-filled beyond statuses)
- Large datasets: `nigeria_ride_data.csv`, `synthetic_training_data.csv`
- Pickle files themselves (only loading code added)
- `campusride/settings.py` core (DEBUG, ALLOWED_HOSTS, etc.)
- `keke_detail` / many polished HTML shells (banner added on driver active; destination picker for **campus stop** still useful for rider intent)
- `set_destination.html` file may still exist on disk but is **not linked** in urls

## QA pass 2 (Tasks 3–6)

### Task 3 — Optimizer business logic
- Documented MVP decision: single-rider marginal detour vs fixed anchor is acceptable at DEFAULT_TOTAL_SEATS=3; full 2-opt / insertion-cost feedback deferred.
- Added `rides/test_optimizer.py`: revenue consistency across greedy/MILP/final recompute; seat-cap; multi-stop nearest-neighbour route shape; marginal-detour independence.

### Task 4 — HTTP/template smoke
- Added `rides/test_http_smoke.py` UrlSmokeTests covering anonymous / rider / driver key URLs, trip_completed, api_active_trips.
- Confirmed `set_destination` has no URL reverse and no template `{% url %}` references.

### Task 5 — Auth & security
- Authorization tests: cross-driver end_trip 404, foreign QR display 404, foreign boarding blocked.
- Login wrong-password does not create a session.
- Documented known username-enumeration via distinct error strings (MVP limitation; production should unify message + rate-limit).
- Sequential same-username signup collision does not 500 / does not create duplicate User rows.
- Production checklist left explicit: DEBUG=True, ALLOWED_HOSTS=['*'], hardcoded SECRET_KEY must become env-driven before deploy.

### Task 6 — Data model / migration edge cases
- TripHistory decision: **wire on end_trip** (update_or_create with total_riders + duration). Model is no longer unused.
- Cold-start test: empty CampusLocation table → driver_go_online seeds defaults and creates a trip.
- choose_anchor_location([]) returns None safely.
- Destination null access already guarded in views/optimizer/templates (destination_id checks / {% if trip.destination %}).

### Concurrency follow-ups from Task 2 open items
- `api_request_seat` now `@retry_on_db_lock` + creates the RideRequest under `select_for_update` on the trip row so last-seat races cannot both insert.
- `driver_go_online` serializes the “no active trip → create” check under lock + retry (closes double-tap Go Online race).
- Default retry budget raised to 8 attempts / 0.06s base.

### Task 1 leftover
- `requirements.txt`: `scikit-learn>=1.3,<2.0` upper bound so a future major bump forces a re-audit/retrain.

## GPS fix pass (v4)
- Root cause of "GPS not working at all": browser Geolocation API is **blocked on non-secure origins**. Opening via `http://192.168.x.x:8000` from a phone → permanent failure. Must use `localhost` or HTTPS (ngrok / Cloudflare Tunnel).
- Driver map, rider map, keke detail, set_destination:
  - Detect `!window.isSecureContext` and show clear message.
  - Two-stage fix: fast network location first (`enableHighAccuracy:false`), then high-accuracy GPS.
  - Explicit error codes (PERMISSION_DENIED / TIMEOUT / POSITION_UNAVAILABLE).
  - **"Use campus test location"** button so you can always proceed without real GPS.
  - Rider seat request no longer hard-fails; offers test coords after GPS failure.
- Campus center fallback remains 7.4436, 3.8970 (geofence still always-allow for testing).


## GPS fix pass (v5)
- Reworked all active GPS flows to request a fresh high-accuracy device position first.
- Added `watchPosition()` to continuously accept improved live fixes instead of relying on one cached reading.
- Set `maximumAge: 0` so stale cached positions are not accepted as the initial fix.
- Added accuracy-aware updates: a materially worse subsequent reading does not overwrite a better fix.
- Removed automatic submission of the campus fallback after a GPS timeout/failure.
- Rider map can still render the campus area when GPS is unavailable, but it no longer sends the campus center as the user's location unless the user explicitly chooses the test-location button.
- Driver and rider GPS coordinates are written to forms/API payloads with 8 decimal places.
- Improved secure-context diagnostics so an HTTP LAN URL clearly explains why browser GPS is unavailable.
- `set_destination.html` was updated too, even though it is currently not linked, so any future use gets the same GPS behavior.
- Validation: Python source files compile successfully; modified JavaScript blocks pass Node syntax checks. Full Django test suite could not be executed in this environment because Django dependencies are not installed and outbound package installation is unavailable.
