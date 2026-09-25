# Concert Finder

A Django app that logs into a user's Spotify account, lists their top
artists and followed artists, checks which of their followed artists are
currently on tour, and searches for followed artists' upcoming shows near
a given city within a radius and date range.

Concert/tour data comes from the **Ticketmaster Discovery API**, not
Spotify — Spotify retired its own concerts endpoints, and Ticketmaster's
API conveniently accepts a city name + radius + date range directly.

## Setup

```bash
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in:

- **Spotify**: create an app at the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard),
  add `http://127.0.0.1:8000/callback/` as a Redirect URI on that app, then
  copy the Client ID and Client Secret into `.env`.
- **Ticketmaster**: get a free API key at the
  [Ticketmaster Developer Portal](https://developer.ticketmaster.com/) and
  put it in `TICKETMASTER_API_KEY`.

Then:

```bash
python manage.py migrate
python manage.py runserver
```

Visit http://127.0.0.1:8000/.

## How it maps to the requirements

1. **Log in with Spotify** — `/login/` starts the OAuth Authorization
   Code flow; `/callback/` exchanges the code for tokens, stored in the
   session (with silent refresh via the refresh token on every request
   that needs Spotify).
2. **List favorite/followed artists** — `/artists/` shows the user's
   Spotify top artists (`GET /me/top/artists`, read-only); `/followed/`
   shows artists the user actually follows (`GET /me/following`), with
   an Unfollow button on each (`DELETE /me/following`). Both pages are
   paginated using `ARTISTS_PER_PAGE`.
3. **Artists on tour + schedule** — `/on-tour/` checks each *followed*
   artist against Ticketmaster (`/attractions.json`, using its
   `upcomingEvents` count) and lists only those with upcoming shows;
   clicking one goes to `/artist/<id>/`, which resolves the artist's name
   from Ticketmaster (`/attractions/<id>.json`) and lists that performer's
   full upcoming schedule (`/events.json?attractionId=...`).
4. **City + radius + date range search** — `/search/` takes a city,
   radius, unit (miles/km) and date range. The city name is geocoded to
   coordinates first (`concerts/geocoding.py`, via OpenStreetMap's free
   Nominatim service) and searched against Ticketmaster's `/events.json`
   using `geoPoint` (a geohash, `concerts/geohash.py`) + `radius` — a real
   geographic radius search. Results are filtered down to shows featuring
   one of the user's *followed* artists. An optional checkbox also pulls
   in artists from the user's Liked Songs (`GET /me/tracks`, paginated,
   deduplicated by artist ID) and matches against those too — off by
   default since scanning a large Liked Songs library is slow (one
   request per 50 tracks, capped by `LIKED_SONGS_SCAN_LIMIT`).

## Notes / limitations

- "Favorite artists" (`/artists/`) = Spotify's top artists (medium-term
  listening history) and is read-only — Spotify has no API to edit that
  algorithmically-computed list. "Followed artists" (`/followed/`) is the
  real, editable list, which is why On Tour and City Search are driven by
  followed artists rather than top artists: unfollowing there has a real,
  visible effect on the list.
- City search uses `geoPoint` rather than Ticketmaster's `city` parameter
  on purpose: `city` is a literal string match against however the venue
  happens to be filed (e.g. a venue stored under "Tel Aviv - Jaffa" won't
  match a search for "Tel Aviv"), so it silently misses real events
  outside Ticketmaster's primary US/Canada markets. Geocoding first and
  searching by coordinates finds events regardless of the venue's city
  spelling. City coordinates are cached for 30 days per city name.
- Matching a Spotify artist to a Ticketmaster "attraction" is done by
  fuzzy name matching (`difflib`), since there's no shared artist ID
  between the two APIs. This is cached in-process for 12 hours per artist
  name to cut down on repeat lookups.
- Ticketmaster's free tier is rate-limited (5 requests/sec, 5000/day), so
  `/on-tour/` (which checks every followed artist one at a time) can take
  a while for users who follow a lot of artists. That check runs in a
  background thread (`concerts/views.py::_run_on_tour_check`), with
  progress stored in Django's cache and polled by the page
  (`/on-tour/progress/<job_id>/`) to drive a live progress bar. This
  relies on the default `LocMemCache`, which is per-process — fine for
  `manage.py runserver`, but would need a shared cache backend (e.g.
  Redis) behind a multi-process production server.
- No database models are used for auth — Spotify tokens live in the
  Django session, so logging out just clears the session.
- Adding a new OAuth scope (as with the Liked Songs feature's
  `user-library-read`) only takes effect for new logins — anyone with an
  existing session needs to log out and back in once to re-grant the
  broader permission before that feature will work for them.
