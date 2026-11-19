"""Where is the sun right now?

Low-precision solar position formulas from the US Naval Observatory (good to about
0.01 degrees, far finer than a wallpaper needs).
"""

import math
from datetime import datetime


def subsolar_point(when: datetime) -> tuple[float, float]:
    """(lat, lon) in degrees of the spot on Earth where the sun is directly overhead.

    `when` must be timezone-aware, e.g. datetime.now(timezone.utc).
    Latitude swings between about +23.44 (June) and -23.44 (December);
    longitude sweeps west 15 degrees per hour.
    """
    if when.tzinfo is None:
        raise ValueError("subsolar_point needs a timezone-aware datetime")

    n = when.timestamp() / 86400.0 + 2440587.5 - 2451545.0   # days since 1 Jan 2000, 12:00 UTC

    mean_longitude = (280.460 + 0.9856474 * n) % 360
    mean_anomaly = math.radians((357.528 + 0.9856003 * n) % 360)
    ecliptic_longitude = math.radians(
        mean_longitude + 1.915 * math.sin(mean_anomaly) + 0.020 * math.sin(2 * mean_anomaly)
    )
    tilt = math.radians(23.439 - 0.0000004 * n)                # Earth's axial tilt

    declination = math.asin(math.sin(tilt) * math.sin(ecliptic_longitude))      # = the sun's latitude
    right_ascension = math.degrees(
        math.atan2(math.cos(tilt) * math.sin(ecliptic_longitude), math.cos(ecliptic_longitude))
    )
    greenwich_sidereal = (280.46061837 + 360.98564736629 * n) % 360          # Earth's spin

    lon = ((right_ascension - greenwich_sidereal + 180) % 360) - 180
    return math.degrees(declination), lon
