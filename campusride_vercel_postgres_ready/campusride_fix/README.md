# CampusRide OR MVP — System-planned revenue-max trips

**Live deployment:** https://campusridefix.vercel.app/

## Product rules
- Driver does **not** pick a destination (Go online → system plans).
- Rider picks only a **fixed campus stop** + GPS; does **not** design the route.
- Objective: **maximize expected shared-fare revenue per trip**.
- Layers online: Matching → ML (demand / no-show) → Optimize → Fares.

## Why this exists: how campus keke transport actually works

This isn't a rideshare app with an unusual matching rule bolted on — it's modeling how tricycle (keke) transport already works informally on Nigerian campuses, and giving that existing pattern proper tooling:

- **Kekes already run fixed low capacity (~3 passengers) and already split fares informally** — a keke driver doesn't wait for one passenger to hire the whole vehicle; they pick up whoever's headed the same general way until full, and passengers already expect to pay a shared per-seat rate, not a private-hire fare. `DEFAULT_TOTAL_SEATS = 3` and the equal-split fare pool in `fares.py` aren't a product decision copied from ride-hailing — they're just encoding what a keke already does.
- **Drivers don't get hailed to a specific address** — they loop known campus routes and informal stopping points, picking up along the way. That's why the driver doesn't pick a destination here either: the system's "anchor" concept mirrors the informal fixed stop a keke driver is already heading toward, not a destination someone typed into an app.
- **Riders pick a fixed campus stop, not a pin on a map** — because that's how boarding a keke actually works: you wait at a known stop, not at your exact GPS location, and the keke comes to where the flow of passengers already is.
- **The optimization problem is real, not decorative**, because a keke has so few seats that *which 3 riders you accept* meaningfully changes the trip's economics — with only 3 seats, picking the wrong 3 out of 6 pending requests can be the difference between a trip that's worth running and one that isn't. That's a fundamentally different problem from what apps like Uber or Bolt solve, where each vehicle typically serves one rider (or one party) at a time and the matching question is just "which nearby driver," not "which subset of several waiting passengers."

## Pipeline

Four layers run in sequence every time a trip needs (re)planning — when a driver goes online, and again every time a rider requests a seat:

```
1. Demand ML   →  choose the system's shared "anchor" destination
2. Matching    →  hard-filter every pending request for geometric feasibility
3. No-show ML  →  score each feasible request's probability of actually boarding
4. Optimize    →  pick the revenue-maximizing subset of requests, within seat capacity
5. Fares       →  split the resulting trip's revenue pot equally among accepted riders
```

This whole sequence lives in `planning.plan_trip()`, which is called both from `create_trip_for_driver()` (when a driver goes online) and from the seat-request API (any time a new pending request might change the optimal accept set).

### 1. Choosing the anchor (`ml_service.choose_anchor_location`)

Because the driver never picks a destination, the system has to pick one for them the moment they go online. This isn't random — it's demand-weighted:

- `predict_demand(hour, day)` returns a relative "busyness" score for the current time. If a trained `demand_model.pkl` (scikit-learn) is present and loadable, its prediction is used; otherwise a calibrated heuristic kicks in — campus mornings (7–9am) and late afternoons (4–6pm) score highest, midday scores medium, everything else is baseline. The heuristic's peak-hour boost is itself calibrated from `calibration_stats.pkl` rather than hardcoded, so it can be retrained without touching code.
- That demand score is converted into a **stable index** (`floor(demand * 10) % len(locations)`) into the list of campus locations, so the anchor choice is deterministic for a given time-of-day rather than random — the same conditions produce the same anchor.
- One subtle correctness fix baked into this function: if a candidate anchor sits within `MIN_ANCHOR_DISTANCE_METERS` (250m) of the driver's own position, it's excluded when a farther option exists. This matters because every downstream feasibility check compares *direction* — driver→anchor vector vs pickup→dropoff vector — and a near-zero-length reference vector makes that comparison numerically meaningless (a rider going in literally any direction would look "aligned" with a vector that has no real direction).

### 2. Matching: hard feasibility gate (`matching.evaluate_rider`)

Before any ML or optimization runs, every pending request is checked against two independent geometric tests. Both must pass, or the request never becomes a candidate:

**a) Marginal detour** — `marginal_detour_meters()` computes the classic ride-pooling insertion cost:

```
detour = dist(O→P) + dist(P→D) + dist(D→Z) − dist(O→Z)
```

where O = driver start, Z = anchor, P = rider pickup, D = rider destination. This is "how much extra distance does picking up and dropping off this rider add to the driver's trip, compared to going straight to the anchor." If it exceeds `MAX_DETOUR_METERS` (800m), the code doesn't reject outright — it falls back to a softer perpendicular-distance-to-segment check (`distance_point_to_segment`), which asks "is the pickup point still roughly *on the way*, even if the O→P→D→Z path is long." Only if both checks fail is the rider marked infeasible.

**b) Direction alignment** — `direction_alignment()` computes the cosine similarity between the driver's overall direction vector (O→Z) and the rider's own trip vector (P→D). A rider heading in a compatible direction scores close to 1; a rider heading the opposite way scores close to −1 and is rejected below `MIN_ALIGNMENT_THRESHOLD` (0.55). This is what stops the system from accepting a rider who happens to be geometrically "on the way" by distance alone but is actually trying to go the opposite direction along that path.

If the driver→anchor reference vector itself is too short (`< MIN_REFERENCE_VECTOR_METERS`, 50m — e.g. anchor picked very close to the driver even after the exclusion above), the alignment check is skipped entirely rather than penalizing the rider for a direction comparison that wouldn't mean anything, and the score falls back to detour-only.

Requests that pass both checks get a composite score:

```
score = 0.5 × (1 − detour/MAX_DETOUR) + 0.5 × max(alignment, 0)
```

— rewarding both shortness of detour and closeness of direction, in equal weight.

### 3. No-show prediction (`ml_service.predict_p_board`)

Every feasible request also gets a **probability of actually boarding**, `p_board`, computed from five features: minutes waited so far, detour in km, hour of day, day of week, and distance from the driver to the rider's pickup point. If a trained logistic model (`no_show_model.pkl`) is available, its `predict_proba` output is used directly. If not, a hand-calibrated heuristic applies: longer waits, bigger detours, and greater driver-to-rider distance all push the probability down, clamped to a sane [0.2, 0.95] range so no request is ever treated as a guaranteed no-show or a guaranteed board.

This number isn't cosmetic — it directly multiplies into the optimizer's objective function in the next step, which is what makes "expected revenue" genuinely expected-*value*, not just fare-sum.

### 4. Optimization: picking the accept set (`optimizer.py`)

This is the core OR problem. Given the surviving feasible, scored candidates and a hard seat-capacity constraint, the system picks which subset to accept:

```
maximize   Σ (fare_i × p_board_i) × x_i
subject to Σ x_i ≤ seats_available
           x_i ∈ {0, 1}
```

Two solvers implement this, in a fallback chain:

- **MILP (`_milp_select`)** — built with Pyomo as a proper binary integer program, solved via whichever of CBC/GLPK/SCIP is available on the machine. This is the "correct" optimal solution to the knapsack-style problem above.
- **Greedy (`_greedy_select`)** — always available with zero external dependencies. Sorts candidates by their individual expected revenue (`fare × p_board`) descending and takes the top `seats` of them. Because this is a single-knapsack-style selection where each item's "weight" is exactly 1 seat, greedy-by-value is actually *provably optimal* here too, not just an approximation — the MILP path exists mainly for when the constraint structure grows more complex later (e.g. multi-resource constraints), and to keep an exact/heuristic pair for cross-checking during development. `optimizer.optimize_trip()` always tries MILP first and transparently falls back to greedy if Pyomo or a solver binary isn't installed — the whole ML+optimization pipeline works with zero solver setup.

The winning method is recorded per-trip in `plan_explanation` (e.g. `Method=greedy; accepted [alice, bob]; pool=₦1200; expected=₦1080; share=₦600/rider`), so every accept decision is auditable after the fact.

**Concurrency correctness:** `optimize_trip()` wraps the entire read→select→write cycle in one `transaction.atomic()` block with `select_for_update()` locking the `ActiveTrip` row first. This closes a real race condition — without it, two simultaneous requests for a trip's last seat could both read the same stale `seats_available`, both compute a selection based on it, and both write "accepted," oversubscribing the vehicle. On Postgres/MySQL this is a genuine row lock; on SQLite (used for local dev) Django downgrades `select_for_update()` to a no-op, but the surrounding `atomic()` block still gets SQLite's database-level write lock, so concurrent calls still serialize correctly — the second caller simply blocks until the first commits, then re-reads the now-current seat count.

### 5. Fares: the shared-revenue pot (`fares.py`)

Once the accept set is finalized, each accepted rider's individual leg fare (`leg_fare()`, straight-line distance × a calibrated ₦/km rate pulled from `calibration_stats.pkl`, with a hardcoded fallback) is summed into a single **trip revenue pool** (`trip_revenue_pool`). That pool is then **split equally** across every accepted rider (`shared_shares`) — so riders on the same trip pay the same amount regardless of how far their individual leg was, which is the "shared-fare" part of the pricing model. This equal-split rule is intentionally simple for the MVP; the `expected_revenue()` function used inside the optimizer's objective still weights each rider's *contribution* by their own `p_board`, so the optimizer's selection decision is fare-aware per rider even though the final payout is pooled and equal.

## Quick start

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

Open http://127.0.0.1:8000/login/ on the same computer.
- Staff → Driver → **Go online**
- Student → Rider map → request seat (choose campus stop)

### GPS / phone testing

Browser geolocation is a security-sensitive API. On a phone, do **not** open the app as
`http://192.168.x.x:8000`; modern browsers block geolocation on that non-secure origin.
Use `http://localhost:8000` on the same device, or expose Django through an HTTPS tunnel
such as ngrok/Cloudflare Tunnel.

The app requests a **fresh high-accuracy device position first** (`enableHighAccuracy: true`,
`maximumAge: 0`) and keeps listening with `watchPosition()` for better fixes. A campus
coordinate is never silently submitted as the rider/driver's real location. The **Use campus
test location** button is an explicit development-only fallback.

The GPS status shown in the UI includes the latitude, longitude, and browser-reported
accuracy (for example `±12m`). The browser/device ultimately controls the accuracy available:
a phone may use GNSS/GPS plus Wi-Fi/cell positioning, while a desktop may only provide
network-based positioning.

## Optional

```bash
python manage.py optimize_all   # replan all active trips
python manage.py createsuperuser
```

## QA status (post Tasks 3–6)
- Full suite: `python manage.py test rides` → 47/47 passing.
- Optimizer MVP decision: marginal detour vs fixed anchor is intentional at 3 seats; 2-opt deferred (see `rides/test_optimizer.py` docstring).
- TripHistory is written on `end_trip`.
- Production must flip `DEBUG`, `ALLOWED_HOSTS`, and `SECRET_KEY` to env-driven values.

## Vercel + PostgreSQL deployment

This project is configured for Vercel's Django runtime, and is currently live at
https://campusridefix.vercel.app/. Vercel detects Django from `manage.py` and a
`[project]` table in `pyproject.toml`. The app uses SQLite only when `DATABASE_URL`
is absent and switches to PostgreSQL automatically when `DATABASE_URL` is present.

1. In Vercel, set the **Root Directory** to the exact folder containing `manage.py`.
2. Provision a managed Postgres database (this project uses Supabase) and set
   `DATABASE_URL` as a Vercel environment variable. **Use the pooled/transaction
   connection string** (host containing `.pooler.supabase.com`, port `6543`),
   not the direct connection — Vercel's serverless functions can fail to reach
   direct-connection IPv6 addresses.
3. Add `DJANGO_SECRET_KEY` as a Vercel environment variable, set to a fresh random
   value, applied to **Production and Preview** environments (not just Development).
   Generate one with `python -c "import secrets; print(secrets.token_urlsafe(50))"`.
4. Leave Output Directory as N/A. Vercel detects Django automatically once
   `pyproject.toml` declares a valid `[project]` table.
5. The Vercel build step runs `python manage.py migrate --noinput` (configured via
   `[tool.vercel.scripts]` in `pyproject.toml`).
6. Static assets are served through WhiteNoise.
7. Don't override the default Install Command — a custom install command disables
   Vercel's automatic function-bundle optimization and can push the deployed
   bundle over the 225–250 MB serverless size limit (a real risk here given
   `numpy`, `scikit-learn`, and `pyomo` in `requirements.txt`).

For local development, set `DJANGO_DEBUG=true` and a local `DJANGO_SECRET_KEY`,
then run `python manage.py migrate` followed by `python manage.py runserver`.
