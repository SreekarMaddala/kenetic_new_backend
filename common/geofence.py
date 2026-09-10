import math
from typing import Tuple


def calculate_haversine_distance(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """
    Calculates great-circle distance between two geographic coordinates in meters.
    Uses Haversine formula.
    """
    earth_radius_m = 6371000.0

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

    return earth_radius_m * c


def is_within_geofence(
    user_lat: float,
    user_lon: float,
    site_lat: float,
    site_lon: float,
    radius_meters: float = 500.0,
) -> Tuple[bool, float]:
    """
    Verifies if a user coordinate is within the permitted radius of a site.
    Returns (is_valid, actual_distance_meters).
    """
    distance = calculate_haversine_distance(user_lat, user_lon, site_lat, site_lon)
    return distance <= radius_meters, round(distance, 2)
