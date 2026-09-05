# CampusRide OR MVP — System-planned revenue-max trips

## Product rules
- Driver does **not** pick a destination (Go online → system plans).
- Rider picks only a **fixed campus stop** + GPS; does **not** design the route.
- Objective: **maximize expected shared-fare revenue per trip**.
- Layers online: Matching → ML (demand / no-show) → Optimize → Fares.

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

The app now requests a **fresh high-accuracy device position first** (`enableHighAccuracy: true`,
`maximumAge: 0`) and keeps listening with `watchPosition()` for better fixes. A campus
coordinate is never silently submitted as the rider/driver's real location. The **Use campus
test location** button is an explicit development-only fallback.

The GPS status shown in the UI includes the latitude, longitude, and browser-reported
accuracy (for example `±12m`). The browser/device ultimately controls the accuracy available:
a phone may use GNSS/GPS plus Wi-Fi/cell positioning, while a desktop may only provide
network-based positioning.

## Pipeline
1. Demand ML chooses system **anchor**
2. Matching filters feasible riders
3. No-show ML scores `p_board`
4. Optimize maximizes expected revenue (greedy always; MILP if CBC/GLPK present)
5. Shared fares split the trip pot

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

This project is configured for Vercel's Django runtime. Vercel detects Django
from `manage.py`. The app uses SQLite only when `DATABASE_URL` is absent and
switches to PostgreSQL automatically when `DATABASE_URL` is present.

1. In Vercel, set the Root Directory to the folder containing `manage.py`.
2. Add a managed PostgreSQL database through Vercel Marketplace/Storage and
   make sure `DATABASE_URL` is available to the deployment.
3. Add `DJANGO_SECRET_KEY` as a Vercel environment variable. Generate a new,
   random value; do not commit the old development key.
4. Leave Output Directory as N/A. Vercel can detect Django automatically.
5. The Vercel build hook runs `python manage.py migrate --noinput`.
6. Static assets are served through WhiteNoise.

For local development, set `DJANGO_DEBUG=true` and a local `DJANGO_SECRET_KEY`,
then run `python manage.py migrate` followed by `python manage.py runserver`.
