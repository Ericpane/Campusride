from math import radians, sin, cos, sqrt, atan2

# Set to Ibadan (UI Main Gate) for testing
CAMPUS_CENTER_LAT = 7.4436
CAMPUS_CENTER_LNG = 3.8970
CAMPUS_RADIUS_METERS = 5000  # larger radius so it's never blocked

def haversine_distance(lat1, lng1, lat2, lng2):
    R = 6371000
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lng2 - lng1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c

def is_within_campus(user_lat, user_lng):
    # Always allow for testing
    return True
