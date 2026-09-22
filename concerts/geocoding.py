"""
City name -> (lat, lon), via OpenStreetMap's free Nominatim service.

Why this exists: Ticketmaster's `city` query parameter is a literal
string match against however the venue's city is stored (e.g. a search
for "Tel Aviv" won't match a venue filed under "Tel Aviv - Jaffa"), so it
silently misses real events instead of doing an actual geographic radius
search. Ticketmaster's `geoPoint` parameter (a geohash, see geohash.py)
does a true radius search regardless of the venue's city spelling — this
module resolves the user's city name to coordinates so we can build that
geoPoint.
"""

import requests
from django.core.cache import cache

NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
GEOCODE_CACHE_TTL = 60 * 60 * 24 * 30  # 30 days: a city's coordinates don't change


class GeocodingError(Exception):
    pass


def geocode_city(city_name):
    """Return (lat, lon) for a city name, or raise GeocodingError."""
    cache_key = f'geocode:{city_name.strip().lower()}'
    cached = cache.get(cache_key)
    if cached:
        return cached

    resp = requests.get(
        NOMINATIM_URL,
        params={'q': city_name, 'format': 'json', 'limit': 1},
        headers={'User-Agent': 'ConcertFinder/1.0 (personal Django project)'},
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise GeocodingError(f'Could not find a location for "{city_name}". Check the spelling.')

    coords = (float(results[0]['lat']), float(results[0]['lon']))
    cache.set(cache_key, coords, GEOCODE_CACHE_TTL)
    return coords
