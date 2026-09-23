"""
Thin wrapper around the Ticketmaster Discovery API v2.

Why Ticketmaster and not Spotify for concert data: Spotify retired its
own "artist concerts" endpoints years ago, so tour/event data has to come
from a separate provider. Ticketmaster's Discovery API was picked because
it's free to sign up for at developer.ticketmaster.com.

Note on location search: Ticketmaster's `city` query parameter is a
literal string match against the venue's stored city name (e.g. a search
for "Tel Aviv" won't match a venue filed under "Tel Aviv - Jaffa"), not a
real geocoded radius search, so it silently misses real events. Real
radius search uses `geoPoint` (a geohash) instead, which is why
search_events_by_location geocodes the city name itself (see
geocoding.py) rather than passing the city straight through.

Docs: https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/
"""

import difflib

import requests
from django.conf import settings
from django.core.cache import cache

from . import geocoding, geohash

DISCOVERY_BASE = 'https://app.ticketmaster.com/discovery/v2'
ATTRACTION_CACHE_TTL = 60 * 60 * 12  # 12 hours
REQUEST_TIMEOUT = 10


class TicketmasterConfigError(Exception):
    pass


def _require_api_key():
    if not settings.TICKETMASTER_API_KEY:
        raise TicketmasterConfigError(
            'TICKETMASTER_API_KEY is not set. Add it to your .env file.'
        )
    return settings.TICKETMASTER_API_KEY


def _get(path, params):
    params = dict(params)
    params['apikey'] = _require_api_key()
    resp = requests.get(f'{DISCOVERY_BASE}{path}', params=params, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 429:
        raise requests.HTTPError('Ticketmaster rate limit exceeded, try again shortly.')
    resp.raise_for_status()
    return resp.json()


def _best_attraction_match(artist_name, attractions):
    """Pick the attraction whose name most closely matches the artist name."""
    if not attractions:
        return None
    names = [a['name'] for a in attractions]
    close = difflib.get_close_matches(artist_name, names, n=1, cutoff=0.6)
    if close:
        chosen_name = close[0]
    else:
        # Fall back to a case-insensitive exact match, else just the first result.
        exact = [n for n in names if n.lower() == artist_name.lower()]
        chosen_name = exact[0] if exact else names[0]
    for a in attractions:
        if a['name'] == chosen_name:
            return a
    return attractions[0]


def find_attraction_for_artist(artist_name):
    """
    Look up the Ticketmaster "attraction" (performer) record that best
    matches a Spotify artist name. Returns a dict with tour status, or
    None if Ticketmaster has no matching performer. Results are cached
    since the same artist gets looked up on every "on tour" check.
    """
    cache_key = f'tm_attraction:{artist_name.strip().lower()}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached or None

    data = _get('/attractions.json', {
        'keyword': artist_name,
        'classificationName': 'Music',
        'size': 5,
    })
    candidates = data.get('_embedded', {}).get('attractions', [])
    match = _best_attraction_match(artist_name, candidates)

    result = None
    if match:
        upcoming = match.get('upcomingEvents', {})
        total_upcoming = upcoming.get('_total', 0)
        result = {
            'id': match['id'],
            'name': match['name'],
            'url': match.get('url'),
            'image': (match.get('images') or [{}])[0].get('url'),
            'upcoming_event_count': total_upcoming,
            'on_tour': total_upcoming > 0,
        }

    cache.set(cache_key, result if result else False, ATTRACTION_CACHE_TTL)
    return result


def _format_event(event):
    venue = {}
    venues = event.get('_embedded', {}).get('venues', [])
    if venues:
        v = venues[0]
        venue = {
            'name': v.get('name'),
            'city': (v.get('city') or {}).get('name'),
            'state': (v.get('state') or {}).get('stateCode'),
            'country': (v.get('country') or {}).get('countryCode'),
            'address': (v.get('address') or {}).get('line1'),
        }

    dates = event.get('dates', {}).get('start', {})
    attractions = event.get('_embedded', {}).get('attractions', [])

    return {
        'id': event.get('id'),
        'name': event.get('name'),
        'url': event.get('url'),
        'date': dates.get('localDate'),
        'time': dates.get('localTime'),
        'time_tbd': dates.get('timeTBA', False) or dates.get('noSpecificTime', False),
        'venue': venue,
        'artist_names': [a.get('name') for a in attractions],
        'image': (event.get('images') or [{}])[0].get('url'),
    }


def get_attraction(attraction_id):
    """
    Fetch a single attraction (performer) by its Ticketmaster ID. Used to
    resolve the artist's display name from an ID alone, since names can
    contain characters (like "/") that aren't safe to carry through a URL.
    """
    try:
        match = _get(f'/attractions/{attraction_id}.json', {})
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise

    upcoming = match.get('upcomingEvents', {})
    return {
        'id': match['id'],
        'name': match['name'],
        'url': match.get('url'),
        'image': (match.get('images') or [{}])[0].get('url'),
        'upcoming_event_count': upcoming.get('_total', 0),
        'on_tour': upcoming.get('_total', 0) > 0,
    }


def get_events_for_attraction(attraction_id, size=50):
    """All upcoming events (tour dates) for a single performer."""
    data = _get('/events.json', {
        'attractionId': attraction_id,
        'sort': 'date,asc',
        'size': size,
    })
    events = data.get('_embedded', {}).get('events', [])
    return [_format_event(e) for e in events]


def search_events_by_location(city, radius, unit, start_date, end_date, max_pages=5, page_size=200):
    """
    All music events within `radius` (unit: 'miles' or 'km') of `city`
    between start_date and end_date (YYYY-MM-DD strings). `city` is
    geocoded to coordinates first (see geocoding.py) and searched via
    Ticketmaster's `geoPoint`, since Ticketmaster's own `city` filter is a
    literal name match, not a real radius search. Raises
    geocoding.GeocodingError if the city name can't be resolved.
    """
    lat, lon = geocoding.geocode_city(city)
    geo_point = geohash.encode(lat, lon, precision=9)

    all_events = []
    start_dt = f'{start_date}T00:00:00Z'
    end_dt = f'{end_date}T23:59:59Z'

    for page in range(max_pages):
        data = _get('/events.json', {
            'geoPoint': geo_point,
            'radius': radius,
            'unit': unit,
            'startDateTime': start_dt,
            'endDateTime': end_dt,
            'classificationName': 'Music',
            'sort': 'date,asc',
            'size': page_size,
            'page': page,
        })
        events = data.get('_embedded', {}).get('events', [])
        all_events.extend(_format_event(e) for e in events)

        page_info = data.get('page', {})
        total_pages = page_info.get('totalPages', 1)
        if page + 1 >= total_pages:
            break

    return all_events


def filter_events_by_artists(events, artist_names):
    """
    Keep only events whose lineup includes one of the given artist names —
    exact (case-insensitive) matches only.

    This used to also fall back to fuzzy/substring matching for minor name
    variants (e.g. "Bruce Springsteen" vs "Bruce Springsteen & The E Street
    Band"), but in practice that produced far more false positives than
    real matches once scanning hundreds of events against a whole followed
    list: e.g. "manchester orchestra" fuzzy-matched "The Pete Escovedo
    Orchestra" (shared word "orchestra"), and "future islands" matched
    "Elder Island". There's no similarity threshold that reliably tells
    those apart from genuine variants like the Springsteen example above
    — both land in roughly the same similarity range. Precision matters
    more than that occasional recall loss here; official band-name
    variants are still found correctly by the "Who's On Tour" flow, which
    resolves one artist at a time via Ticketmaster's own attraction search
    instead of scanning a big unrelated event list.
    """
    wanted = {n.lower() for n in artist_names}
    matched = []
    for event in events:
        event_artists_lower = {n.lower() for n in event['artist_names'] if n}
        overlap = wanted & event_artists_lower
        if overlap:
            event = dict(event)
            event['matched_favorite_artists'] = sorted(overlap)
            matched.append(event)
    return matched
