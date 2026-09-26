"""
Thin wrapper around the Spotify Web API's Authorization Code flow.

Tokens are kept in the Django session (see views.py) rather than a
database table, so no extra user/auth model is needed: the session *is*
the login. `ensure_valid_token` refreshes an expired access token
in-place before every API call that needs one.
"""

import time
import secrets
import urllib.parse

import requests
from django.conf import settings

AUTHORIZE_URL = 'https://accounts.spotify.com/authorize'
TOKEN_URL = 'https://accounts.spotify.com/api/token'
API_BASE = 'https://api.spotify.com/v1'


class SpotifyAuthError(Exception):
    pass


def build_authorize_url(state=None):
    """Return the URL to send the user to for the Spotify consent screen."""
    state = state or secrets.token_urlsafe(16)
    params = {
        'client_id': settings.SPOTIFY_CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': settings.SPOTIFY_REDIRECT_URI,
        'scope': settings.SPOTIFY_SCOPES,
        'state': state,
    }
    return f'{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}', state


def exchange_code_for_token(code):
    """Exchange an authorization code for an access/refresh token pair."""
    resp = requests.post(
        TOKEN_URL,
        data={
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': settings.SPOTIFY_REDIRECT_URI,
        },
        auth=(settings.SPOTIFY_CLIENT_ID, settings.SPOTIFY_CLIENT_SECRET),
        timeout=10,
    )
    if resp.status_code != 200:
        raise SpotifyAuthError(f'Token exchange failed: {resp.status_code} {resp.text}')
    return resp.json()


def refresh_access_token(refresh_token):
    resp = requests.post(
        TOKEN_URL,
        data={
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
        },
        auth=(settings.SPOTIFY_CLIENT_ID, settings.SPOTIFY_CLIENT_SECRET),
        timeout=10,
    )
    if resp.status_code != 200:
        raise SpotifyAuthError(f'Token refresh failed: {resp.status_code} {resp.text}')
    return resp.json()


def ensure_valid_token(request):
    """
    Make sure request.session['spotify_token'] holds a non-expired access
    token, refreshing it if needed. Returns the access token string, or
    None if the user isn't logged in with Spotify.
    """
    token_data = request.session.get('spotify_token')
    if not token_data:
        return None

    if time.time() >= token_data.get('expires_at', 0):
        refresh_token = token_data.get('refresh_token')
        if not refresh_token:
            return None
        new_data = refresh_access_token(refresh_token)
        token_data['access_token'] = new_data['access_token']
        token_data['expires_at'] = time.time() + new_data.get('expires_in', 3600) - 30
        # Spotify may or may not return a new refresh_token; keep the old one if not.
        if 'refresh_token' in new_data:
            token_data['refresh_token'] = new_data['refresh_token']
        request.session['spotify_token'] = token_data
        request.session.modified = True

    return token_data['access_token']


def _get(access_token, path, params=None):
    resp = requests.get(
        f'{API_BASE}{path}',
        headers={'Authorization': f'Bearer {access_token}'},
        params=params or {},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _delete(access_token, path, params=None):
    resp = requests.delete(
        f'{API_BASE}{path}',
        headers={'Authorization': f'Bearer {access_token}'},
        params=params or {},
        timeout=10,
    )
    resp.raise_for_status()


def _put(access_token, path, params=None):
    resp = requests.put(
        f'{API_BASE}{path}',
        headers={'Authorization': f'Bearer {access_token}'},
        params=params or {},
        timeout=10,
    )
    resp.raise_for_status()


def get_current_user_profile(access_token):
    return _get(access_token, '/me')


def get_top_artists(access_token, time_range='medium_term', limit=50):
    """
    Return the user's top artists (used as the "favorite artists" list),
    ordered by affinity. Spotify caps a single page at 50, so we paginate
    up to `limit`.
    """
    artists = []
    offset = 0
    page_size = 50
    while len(artists) < limit:
        data = _get(
            access_token,
            '/me/top/artists',
            params={
                'time_range': time_range,
                'limit': min(page_size, limit - len(artists)),
                'offset': offset,
            },
        )
        items = data.get('items', [])
        if not items:
            break
        for a in items:
            artists.append({
                'id': a['id'],
                'name': a['name'],
                'genres': a.get('genres', []),
                'image': (a.get('images') or [{}])[0].get('url'),
                'spotify_url': a.get('external_urls', {}).get('spotify'),
                'popularity': a.get('popularity'),
            })
        offset += page_size
        if data.get('next') is None:
            break
    return artists


def get_followed_artists(access_token, limit=50):
    """
    Return the artists the user actually follows on Spotify. Unlike top
    artists, this is a real editable list (see unfollow_artist below), so
    it's the one we let the user remove entries from. The endpoint is
    cursor-paginated rather than offset-paginated.
    """
    artists = []
    after = None
    page_size = 50
    while len(artists) < limit:
        params = {'type': 'artist', 'limit': min(page_size, limit - len(artists))}
        if after:
            params['after'] = after
        data = _get(access_token, '/me/following', params=params)
        block = data.get('artists', {})
        items = block.get('items', [])
        if not items:
            break
        for a in items:
            artists.append({
                'id': a['id'],
                'name': a['name'],
                'genres': a.get('genres', []),
                'image': (a.get('images') or [{}])[0].get('url'),
                'spotify_url': a.get('external_urls', {}).get('spotify'),
                'popularity': a.get('popularity'),
            })
        after = block.get('cursors', {}).get('after')
        if not after:
            break
    return artists


def unfollow_artist(access_token, artist_id):
    _delete(access_token, '/me/following', params={'type': 'artist', 'ids': artist_id})


def follow_artist(access_token, artist_id):
    _put(access_token, '/me/following', params={'type': 'artist', 'ids': artist_id})


def search_artists(access_token, query, limit=5):
    """
    Search Spotify's catalog for artists matching a typed-in name. Needs
    no special scope beyond a valid token, unlike most other calls here.
    """
    data = _get(access_token, '/search', params={'q': query, 'type': 'artist', 'limit': limit})
    items = data.get('artists', {}).get('items', [])
    return [{
        'id': a['id'],
        'name': a['name'],
        'genres': a.get('genres', []),
        'image': (a.get('images') or [{}])[0].get('url'),
        'spotify_url': a.get('external_urls', {}).get('spotify'),
        'popularity': a.get('popularity'),
    } for a in items]


def get_liked_songs_artists(access_token, track_scan_limit=1000, progress_callback=None):
    """
    Return the unique artists behind the user's Liked Songs, by scanning
    up to `track_scan_limit` saved tracks (50 per page, so this is up to
    track_scan_limit/50 requests — the "can take substantial time" part
    of this feature, since a large Liked Songs library means many pages).
    Unlike top/followed artists this has no dedicated "artists" endpoint,
    so artists are derived from each saved track's artist list and
    deduplicated by Spotify artist ID.

    If given, progress_callback(scanned, total) is called after every
    page, where `total` is the smaller of the user's real saved-track
    count (learned from the first page) and track_scan_limit.
    """
    seen_ids = set()
    artists = []
    offset = 0
    page_size = 50
    scanned = 0
    total_estimate = track_scan_limit
    while scanned < track_scan_limit:
        data = _get(
            access_token,
            '/me/tracks',
            params={'limit': min(page_size, track_scan_limit - scanned), 'offset': offset},
        )
        if offset == 0 and data.get('total') is not None:
            total_estimate = min(data['total'], track_scan_limit)
        items = data.get('items', [])
        if not items:
            break
        for item in items:
            track = item.get('track') or {}
            for a in track.get('artists', []):
                if a['id'] in seen_ids:
                    continue
                seen_ids.add(a['id'])
                artists.append({
                    'id': a['id'],
                    'name': a['name'],
                    'spotify_url': a.get('external_urls', {}).get('spotify'),
                })
        scanned += len(items)
        offset += page_size
        if progress_callback:
            progress_callback(min(scanned, total_estimate), total_estimate)
        if data.get('next') is None:
            break
    return artists
