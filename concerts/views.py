import threading
import time
import urllib.parse
import uuid
from functools import wraps

import requests
from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from . import geocoding, spotify_api, ticketmaster_api

# How long a completed/in-progress "on tour" check is kept around so a page
# refresh or the progress poller can find it again instead of starting over.
ON_TOUR_JOB_TTL = 60 * 15

# Same idea for a city search job (see search_by_location / _run_search_job).
SEARCH_JOB_TTL = 60 * 15


def spotify_login_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        access_token = spotify_api.ensure_valid_token(request)
        if not access_token:
            messages.info(request, 'Please log in with Spotify first.')
            return redirect('concerts:home')
        request.spotify_access_token = access_token
        return view_func(request, *args, **kwargs)
    return wrapper


def home(request):
    access_token = spotify_api.ensure_valid_token(request)
    profile = None
    if access_token:
        try:
            profile = spotify_api.get_current_user_profile(access_token)
        except requests.HTTPError:
            profile = None
    return render(request, 'concerts/home.html', {
        'profile': profile,
        'logged_in': access_token is not None,
    })


def spotify_login(request):
    auth_url, state = spotify_api.build_authorize_url()
    request.session['spotify_oauth_state'] = state
    return redirect(auth_url)


def spotify_callback(request):
    error = request.GET.get('error')
    if error:
        messages.error(request, f'Spotify login failed: {error}')
        return redirect('concerts:home')

    state = request.GET.get('state')
    expected_state = request.session.pop('spotify_oauth_state', None)
    if not state or state != expected_state:
        messages.error(request, 'Login could not be verified (state mismatch). Please try again.')
        return redirect('concerts:home')

    code = request.GET.get('code')
    if not code:
        messages.error(request, 'Spotify did not return an authorization code.')
        return redirect('concerts:home')

    try:
        token_data = spotify_api.exchange_code_for_token(code)
    except spotify_api.SpotifyAuthError as exc:
        messages.error(request, str(exc))
        return redirect('concerts:home')

    request.session['spotify_token'] = {
        'access_token': token_data['access_token'],
        'refresh_token': token_data.get('refresh_token'),
        'expires_at': time.time() + token_data.get('expires_in', 3600) - 30,
    }
    messages.success(request, 'Logged in with Spotify!')
    return redirect('concerts:home')


def logout_view(request):
    request.session.pop('spotify_token', None)
    messages.info(request, 'Logged out.')
    return redirect('concerts:home')


@spotify_login_required
def favorite_artists(request):
    artists = spotify_api.get_top_artists(
        request.spotify_access_token, limit=settings.FAVORITE_ARTISTS_LIMIT
    )
    paginator = Paginator(artists, settings.ARTISTS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(request, 'concerts/favorite_artists.html', {
        'page_obj': page_obj,
        'total_artists': len(artists),
    })


@spotify_login_required
def followed_artists(request):
    artists = spotify_api.get_followed_artists(
        request.spotify_access_token, limit=settings.FOLLOWED_ARTISTS_LIMIT
    )
    paginator = Paginator(artists, settings.ARTISTS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get('page'))

    context = {
        'page_obj': page_obj,
        'total_artists': len(artists),
        'search_query': request.GET.get('q', ''),
    }

    query = request.GET.get('q', '').strip()
    if query:
        followed_ids = {a['id'] for a in artists}
        try:
            results = spotify_api.search_artists(request.spotify_access_token, query, limit=5)
            for r in results:
                r['already_following'] = r['id'] in followed_ids
            context['search_results'] = results
        except requests.HTTPError as exc:
            messages.error(request, f'Artist search failed: {exc}')
            context['search_results'] = []

    return render(request, 'concerts/followed_artists.html', context)


@spotify_login_required
def follow_artist(request, artist_id):
    # Deliberately does not carry the search query (`q`) through: after
    # following someone, the user wants the plain followed-artists list
    # back, not the same 5 search results still showing on screen.
    page = request.POST.get('page') or request.GET.get('page')
    redirect_url = reverse('concerts:followed_artists')
    if page:
        redirect_url += '?' + urllib.parse.urlencode({'page': page})

    if request.method != 'POST':
        return redirect(redirect_url)

    artist_name = request.POST.get('artist_name', 'Artist')
    try:
        spotify_api.follow_artist(request.spotify_access_token, artist_id)
        messages.success(request, f'Followed {artist_name} on Spotify.')
    except requests.HTTPError as exc:
        messages.error(request, f'Could not follow {artist_name}: {exc}')

    return redirect(redirect_url)


@spotify_login_required
def unfollow_artist(request, artist_id):
    page = request.POST.get('page') or request.GET.get('page')
    redirect_url = reverse('concerts:followed_artists')
    if page:
        redirect_url += f'?page={page}'

    if request.method != 'POST':
        return redirect(redirect_url)

    artist_name = request.POST.get('artist_name', 'Artist')
    try:
        spotify_api.unfollow_artist(request.spotify_access_token, artist_id)
        messages.success(request, f'Unfollowed {artist_name} on Spotify.')
    except requests.HTTPError as exc:
        messages.error(request, f'Could not unfollow {artist_name}: {exc}')

    return redirect(redirect_url)


def _run_on_tour_check(job_id, access_token, limit):
    """
    Runs in a background thread: fetch followed artists, then check each
    one against Ticketmaster one at a time (this is the slow part — up to
    one HTTP request per artist), updating the cached job progress after
    every artist so the progress bar can poll it.
    """
    try:
        artists = spotify_api.get_followed_artists(access_token, limit=limit)
    except requests.HTTPError as exc:
        cache.set(job_id, {
            'checked': 0, 'total': 0, 'done': True,
            'on_tour_artists': [], 'errors': [f'Could not load followed artists: {exc}'],
        }, ON_TOUR_JOB_TTL)
        return

    total = len(artists)
    on_tour_artists = []
    errors = []
    cache.set(job_id, {
        'checked': 0, 'total': total, 'done': False,
        'on_tour_artists': [], 'errors': [],
    }, ON_TOUR_JOB_TTL)

    for i, artist in enumerate(artists, start=1):
        try:
            attraction = ticketmaster_api.find_attraction_for_artist(artist['name'])
            if attraction and attraction['on_tour']:
                on_tour_artists.append({**artist, 'attraction': attraction})
        except ticketmaster_api.TicketmasterConfigError as exc:
            errors.append(str(exc))
            cache.set(job_id, {
                'checked': i, 'total': total, 'done': True,
                'on_tour_artists': on_tour_artists, 'errors': errors,
            }, ON_TOUR_JOB_TTL)
            return
        except requests.HTTPError as exc:
            errors.append(str(exc))

        cache.set(job_id, {
            'checked': i, 'total': total, 'done': False,
            'on_tour_artists': on_tour_artists, 'errors': errors,
        }, ON_TOUR_JOB_TTL)

    cache.set(job_id, {
        'checked': total, 'total': total, 'done': True,
        'on_tour_artists': on_tour_artists, 'errors': errors,
    }, ON_TOUR_JOB_TTL)


@spotify_login_required
def on_tour(request):
    if request.GET.get('recheck'):
        request.session.pop('on_tour_job_id', None)

    job_id = request.GET.get('job') or request.session.get('on_tour_job_id')
    job = cache.get(job_id) if job_id else None

    if not job:
        job_id = uuid.uuid4().hex
        cache.set(job_id, {'checked': 0, 'total': 0, 'done': False, 'on_tour_artists': [], 'errors': []}, ON_TOUR_JOB_TTL)
        request.session['on_tour_job_id'] = job_id
        threading.Thread(
            target=_run_on_tour_check,
            args=(job_id, request.spotify_access_token, settings.FOLLOWED_ARTISTS_LIMIT),
            daemon=True,
        ).start()
        job = cache.get(job_id)

    if job['done']:
        for err in job['errors']:
            messages.warning(request, err)
        return render(request, 'concerts/on_tour.html', {
            'job_done': True,
            'artists_checked': job['total'],
            'on_tour_artists': job['on_tour_artists'],
        })

    return render(request, 'concerts/on_tour.html', {
        'job_done': False,
        'job_id': job_id,
    })


@spotify_login_required
def on_tour_progress(request, job_id):
    job = cache.get(job_id)
    if not job:
        return JsonResponse({'checked': 0, 'total': 0, 'done': True})
    return JsonResponse({
        'checked': job['checked'],
        'total': job['total'],
        'done': job['done'],
    })


@spotify_login_required
def artist_schedule(request, attraction_id):
    events = []
    artist_name = ''
    try:
        attraction = ticketmaster_api.get_attraction(attraction_id)
        artist_name = attraction['name'] if attraction else 'Unknown artist'
        events = ticketmaster_api.get_events_for_attraction(attraction_id)
    except ticketmaster_api.TicketmasterConfigError as exc:
        messages.error(request, str(exc))
    except requests.HTTPError as exc:
        messages.error(request, f'Could not load tour dates: {exc}')

    return render(request, 'concerts/artist_schedule.html', {
        'artist_name': artist_name,
        'events': events,
    })


def _set_search_job(job_id, **fields):
    state = cache.get(job_id) or {}
    state.update(fields)
    cache.set(job_id, state, SEARCH_JOB_TTL)


def _run_search_job(job_id, access_token, params):
    """
    Runs in a background thread. Two potentially-slow phases, each with
    its own progress reported via a callback: scanning Liked Songs (if
    requested) and paging through Ticketmaster's event search. Matching
    the results against artist names is local/instant, no callback needed.
    """
    try:
        artists = spotify_api.get_followed_artists(access_token, limit=settings.FOLLOWED_ARTISTS_LIMIT)
        artist_names = {a['name'] for a in artists}

        if params['include_liked']:
            _set_search_job(job_id, phase='liked_songs', phase_checked=0, phase_total=1)
            liked_artists = spotify_api.get_liked_songs_artists(
                access_token,
                track_scan_limit=settings.LIKED_SONGS_SCAN_LIMIT,
                progress_callback=lambda scanned, total: _set_search_job(
                    job_id, phase='liked_songs', phase_checked=scanned, phase_total=total
                ),
            )
            artist_names |= {a['name'] for a in liked_artists}

        _set_search_job(job_id, phase='events', phase_checked=0, phase_total=1)
        all_events = ticketmaster_api.search_events_by_location(
            city=params['city'],
            radius=params['radius'],
            unit=params['unit'],
            start_date=params['start_date'],
            end_date=params['end_date'],
            progress_callback=lambda done, total: _set_search_job(
                job_id, phase='events', phase_checked=done, phase_total=total
            ),
        )
        matched_events = ticketmaster_api.filter_events_by_artists(all_events, artist_names)

        _set_search_job(
            job_id, phase='done', phase_checked=1, phase_total=1, done=True, error=None,
            events=matched_events, total_events_scanned=len(all_events),
            artists_checked=len(artist_names),
        )
    except geocoding.GeocodingError as exc:
        _set_search_job(job_id, phase='done', done=True, error=str(exc), events=[])
    except ticketmaster_api.TicketmasterConfigError as exc:
        _set_search_job(job_id, phase='done', done=True, error=str(exc), events=[])
    except requests.HTTPError as exc:
        _set_search_job(job_id, phase='done', done=True, error=f'Search failed: {exc}', events=[])


@spotify_login_required
def search_by_location(request):
    job_id = request.GET.get('job')

    if job_id:
        job = cache.get(job_id)
        if not job:
            messages.error(request, 'That search has expired. Please search again.')
            return redirect('concerts:search')

        params = job.get('params', {})
        if job.get('done'):
            if job.get('error'):
                messages.error(request, job['error'])
            return render(request, 'concerts/search.html', {
                **params,
                'submitted': True,
                'job_done': True,
                'events': job.get('events', []),
                'total_events_scanned': job.get('total_events_scanned', 0),
                'artists_checked': job.get('artists_checked', 0),
            })

        return render(request, 'concerts/search.html', {
            **params,
            'submitted': True,
            'job_done': False,
            'job_id': job_id,
        })

    context = {
        'city': request.GET.get('city', ''),
        'radius': request.GET.get('radius', '25'),
        'unit': request.GET.get('unit', 'miles'),
        'start_date': request.GET.get('start_date', ''),
        'end_date': request.GET.get('end_date', ''),
        'include_liked': request.GET.get('include_liked') == 'on',
        'submitted': False,
    }

    if request.GET.get('city') and request.GET.get('start_date') and request.GET.get('end_date'):
        try:
            radius = int(request.GET.get('radius', 25))
        except ValueError:
            radius = 25

        params = {
            'city': context['city'],
            'radius': radius,
            'unit': context['unit'],
            'start_date': context['start_date'],
            'end_date': context['end_date'],
            'include_liked': context['include_liked'],
        }
        new_job_id = uuid.uuid4().hex
        cache.set(new_job_id, {
            'phase': 'starting', 'phase_checked': 0, 'phase_total': 1,
            'done': False, 'params': params,
        }, SEARCH_JOB_TTL)
        threading.Thread(
            target=_run_search_job,
            args=(new_job_id, request.spotify_access_token, params),
            daemon=True,
        ).start()
        # Redirecting to the job URL (rather than rendering it inline) means
        # a page refresh mid-search resumes polling the same job instead of
        # kicking off a duplicate one.
        return redirect(f"{reverse('concerts:search')}?job={new_job_id}")

    return render(request, 'concerts/search.html', context)


@spotify_login_required
def search_progress(request, job_id):
    job = cache.get(job_id)
    if not job:
        return JsonResponse({'phase': 'done', 'phase_checked': 0, 'phase_total': 1, 'done': True})
    return JsonResponse({
        'phase': job.get('phase', 'starting'),
        'phase_checked': job.get('phase_checked', 0),
        'phase_total': job.get('phase_total', 1),
        'done': job.get('done', False),
    })
