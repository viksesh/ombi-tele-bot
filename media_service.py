"""Shared media search/request logic used by both the Telegram bot and the mini app.

This module owns the Ombi/NZB clients and all surface-agnostic business logic:
IMDb link resolution, query parsing, searching, status detection, auto-approve
rules and request submission. bot.py (messaging) and webapp_server.py (mini app)
are thin presentation layers on top of it.
"""

import os
import re
import time
import logging
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from ombi_client import OmbiClient
from nzb_client import NZBClient

# Load environment variables before reading any config (this module is
# imported before bot.py gets a chance to call load_dotenv itself)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, use system env vars

logger = logging.getLogger(__name__)

# Initialize Ombi client
try:
    ombi_client = OmbiClient()
except ValueError as e:
    logger.error(f"Failed to initialize Ombi client: {e}")
    ombi_client = None

# Initialize NZB client for auto-approve feature
nzb_client = NZBClient()

# Auto-approve user (user with auto-approval rights in Ombi)
OMBI_AUTO_APPROVE_USER = os.getenv('OMBI_AUTO_APPROVE_USER', 'requests-auto').strip()


class ImdbLookupError(Exception):
    """Raised when an IMDb link can't be resolved to a title."""


def _tv_aired_coverage(item: dict) -> Optional[tuple[int, int]]:
    """Count how many aired episodes are covered by the library or Sonarr.

    Ombi's show-level 'available' flag is set as soon as a single episode has a
    file in Sonarr, so it alone can't distinguish fully from partially
    available. Per-episode 'approved' means the episode is monitored in Sonarr
    (or part of an approved request), so any aired episode without it is one
    the server will never grab.

    Specials (season 0) and unaired/undated episodes are excluded — Sonarr
    can't have files for those yet and they'd wrongly downgrade complete shows.

    Returns:
        (aired_count, covered_count), or None if there is no episode data.
    """
    seasons = item.get('seasonRequests')
    if not isinstance(seasons, list) or not seasons:
        return None

    today = datetime.now().strftime('%Y-%m-%d')
    aired = 0
    covered = 0
    for season in seasons:
        if season.get('seasonNumber') == 0:
            continue
        for ep in season.get('episodes') or []:
            air_date = (ep.get('airDate') or '')[:10]
            if not air_date or air_date.startswith('0001') or air_date > today:
                continue
            aired += 1
            if ep.get('available') or ep.get('approved'):
                covered += 1

    if aired == 0:
        return None
    return (aired, covered)


def _upcoming_date(value) -> Optional[str]:
    """Normalize an Ombi date to 'YYYY-MM-DD', or None if missing/past.

    Ombi uses '0001-01-01T00:00:00Z' as its "no date" sentinel.
    """
    if not isinstance(value, str):
        return None
    date = value[:10]
    if len(date) != 10 or date.startswith('0001'):
        return None
    return date if date >= datetime.now().strftime('%Y-%m-%d') else None


def get_expected_date(item: dict, item_type: str) -> Optional[str]:
    """Best estimate of when an item will land in the library, as 'YYYY-MM-DD'.

    Movies use Ombi's digital release date (only present on the detail
    endpoints). TV shows use their premiere date, but ONLY for shows that have
    not aired a single episode yet - a show with any aired episode is either
    already downloaded (available) or being processed, and a future date for it
    would be misleading. Dates in the past are dropped.

    Returns None when no date is known.
    """
    if item_type == 'movie':
        return _upcoming_date(item.get('digitalReleaseDate'))

    seasons = item.get('seasonRequests')
    if not isinstance(seasons, list):
        return None
    air_dates = [
        date
        for season in seasons if season.get('seasonNumber') != 0
        for ep in season.get('episodes') or []
        for date in [(ep.get('airDate') or '')[:10]]
        if len(date) == 10 and not date.startswith('0001')
    ]
    if not air_dates:
        return None
    # Only a not-yet-premiered show gets a date: if its earliest episode has
    # already aired, at least one episode exists to grab, so it isn't "new".
    return _upcoming_date(min(air_dates))


def get_item_status(item: dict):
    """Check item status and return (should_hide_request, status_text).

    For TV shows the denied/requested/approved flags are populated by
    _merge_tv_request_state (the search/detail endpoints don't carry request
    state), so this reads the same top-level flags for movies and TV.

    Returns:
        tuple: (bool, str) - (True if request should be hidden, status message)
        Status can be: 'available', 'partially_available', 'approved', 'requested', 'denied', or None
    """
    # Check if denied first (highest priority - cannot request if denied)
    denied = item.get('denied', False) or item.get('Denied', False) or item.get('isDenied', False)
    if denied:
        logger.debug(f"Item is denied (denied={denied}, deniedReason={item.get('deniedReason')})")
        return (True, 'denied')

    # Check if truly available (in library/content provider)
    if item.get('available', False) or item.get('alreadyincp', False) or item.get('fullyAvailable', False):
        # For TV, 'available' can mean as little as one episode with a file in
        # Sonarr - use per-episode data to downgrade to partially_available
        if not item.get('fullyAvailable', False):
            coverage = _tv_aired_coverage(item)
            if coverage:
                aired, covered = coverage
                if covered < aired:
                    logger.debug(f"Only {covered}/{aired} aired episodes covered - partially available")
                    return (True, 'partially_available')
        return (True, 'available')

    # Check if partially available (some episodes/seasons available)
    # First check the partlyAvailable field
    partly_avail = None
    if 'partlyAvailable' in item:
        partly_avail = item['partlyAvailable']
        logger.debug(f"Found partlyAvailable key with value: {partly_avail} (type: {type(partly_avail)})")
    elif 'partiallyAvailable' in item:
        partly_avail = item['partiallyAvailable']
        logger.debug(f"Found partiallyAvailable key with value: {partly_avail} (type: {type(partly_avail)})")
    elif 'partiallyavailable' in item:
        partly_avail = item['partiallyavailable']
        logger.debug(f"Found partiallyavailable key with value: {partly_avail} (type: {type(partly_avail)})")

    # Also check seasonRequests for TV shows - if some seasons are available, it's partially available
    if 'seasonRequests' in item:
        season_requests = item.get('seasonRequests', [])
        if isinstance(season_requests, list) and len(season_requests) > 0:
            # Check if any seasons are available
            available_seasons = [s for s in season_requests if s.get('available', False) or s.get('fullyAvailable', False)]
            total_seasons = len(season_requests)
            if available_seasons and len(available_seasons) < total_seasons:
                # Some seasons available but not all = partially available
                logger.debug(f"Found {len(available_seasons)}/{total_seasons} seasons available - marking as partially available")
                return (True, 'partially_available')

    # Check if value is truthy (not None, not False, not empty string)
    if partly_avail is not None:
        # If it's explicitly False, don't treat as partially available
        if partly_avail is False:
            logger.debug("partlyAvailable is False - not partially available")
        # If it's an empty string or falsy string, don't treat as partially available
        elif isinstance(partly_avail, str) and partly_avail.strip().lower() in ('false', '0', 'no', ''):
            logger.debug(f"partlyAvailable is falsy string '{partly_avail}' - not partially available")
        # Any other truthy value means partially available (True, 1, "true", non-empty string, etc.)
        else:
            logger.debug(f"Item is partially available (partlyAvailable={partly_avail}, truthy={bool(partly_avail)})")
            return (True, 'partially_available')

    # Check if approved (scheduled to upload when available digitally)
    # Check multiple possible field names and values
    approved = item.get('approved', False) or item.get('Approved', False) or item.get('isApproved', False)
    request_id = item.get('requestId')

    # If there's a requestId, it might be approved
    # Also check if approved is explicitly True
    if request_id:
        logger.debug(f"Item has requestId={request_id}, approved={approved}")
        # If approved is True, definitely approved
        if approved:
            logger.debug("Item is approved (approved=True)")
            return (True, 'approved')
        # If there's a requestId but approved is False, it might still be pending approval
        # But we'll treat it as requested, not approved

    if approved:
        logger.debug(f"Item is approved (approved={approved})")
        return (True, 'approved')

    # Check if requested (pending approval)
    # requestId: 0 or null means no request exists, any positive number means a request exists
    has_request = request_id and request_id != 0 and request_id != '0'
    if item.get('requested', False) or has_request:
        logger.debug(f"Item is requested (requested={item.get('requested')}, requestId={request_id}, has_request={has_request})")
        return (True, 'requested')

    # Not available, approved, or requested - can be requested
    return (False, None)


def get_item_id(item: dict):
    """Extract the canonical request ID from an item (Ombi uses theTvDbId for TV shows)."""
    return (item.get('theTvDbId') or item.get('theMovieDbId') or
            item.get('tvDbId') or item.get('tvdbId') or
            item.get('id') or item.get('Id') or
            item.get('movieDbId') or item.get('movie_id') or
            item.get('tmdbId') or item.get('tmdb_id') or
            item.get('seriesId'))


def get_item_title(item: dict, item_type: str) -> str:
    """Extract the display title from an item."""
    if item_type == 'movie':
        return (item.get('title') or item.get('Title') or
                item.get('movieTitle') or item.get('name') or 'Unknown')
    return (item.get('title') or item.get('Title') or
            item.get('name') or item.get('Name') or
            item.get('seriesName') or item.get('tvShowName') or 'Unknown')


def get_item_overview(item: dict) -> str:
    """Extract the overview/description from an item."""
    return (item.get('overview') or item.get('Overview') or
            item.get('description') or item.get('Description') or
            item.get('plot') or item.get('Plot') or
            'No description available.')


def get_item_year(item: dict, item_type: str) -> int:
    """Extract the year from an item."""
    if item_type == 'movie':
        year = (item.get('releaseDate') or item.get('release_date') or
                item.get('year') or item.get('Year') or
                item.get('releaseYear'))
    else:
        year = (item.get('firstAired') or item.get('firstAirDate') or
                item.get('first_air_date') or item.get('year') or
                item.get('Year') or item.get('releaseYear') or
                item.get('firstAirYear'))

    if year:
        if isinstance(year, str) and len(year) >= 4:
            year_match = re.search(r'(\d{4})', str(year))
            if year_match:
                return int(year_match.group(1))
        elif isinstance(year, int):
            return year
    return 0


def get_item_rating(item: dict, item_type: str) -> float:
    """Extract rating from an item for sorting purposes."""
    if item_type == 'movie':
        rating = (item.get('voteAverage') or item.get('vote_average') or
                  item.get('rating') or item.get('Rating') or
                  item.get('voteRating') or 0)
    else:
        rating = (item.get('rating') or item.get('Rating') or
                  item.get('voteAverage') or item.get('vote_average') or
                  item.get('siteRating') or item.get('voteRating') or 0)
    try:
        rating = float(rating)
        if 0 < rating <= 1:
            rating = rating * 10
        return rating
    except (ValueError, TypeError):
        return 0.0


def get_item_popularity(item: dict, item_type: str) -> float:
    """Extract popularity (vote count) from an item for sorting purposes."""
    # Try vote_count first (TMDB field for popularity)
    vote_count = (item.get('voteCount') or item.get('vote_count') or 0)
    try:
        return float(vote_count)
    except (ValueError, TypeError):
        return 0.0


def get_poster_url(item: dict, base_url: str = "https://image.tmdb.org/t/p/w500") -> str:
    """Get poster URL for item."""
    # Try multiple possible field names for poster path
    # Ombi TV shows use 'banner' for poster images
    poster_path = (item.get('banner') or item.get('Banner') or  # TV shows often use banner
                   item.get('posterPath') or item.get('poster_path') or
                   item.get('backdropPath') or item.get('backdrop_path') or
                   item.get('poster') or item.get('Poster') or
                   item.get('posterUrl') or item.get('poster_url') or
                   item.get('image') or item.get('Image'))

    if poster_path:
        # If it's already a full URL, return it
        if poster_path.startswith('http://') or poster_path.startswith('https://'):
            return poster_path
        # If it starts with /, prepend base URL
        if poster_path.startswith('/'):
            return f"{base_url}{poster_path}"
        # Otherwise, assume it's a relative path
        return f"{base_url}/{poster_path.lstrip('/')}"
    return None


def resolve_imdb_link(query_text: str) -> tuple[str, Optional[int]]:
    """Resolve an IMDb URL to (title, year) using IMDB's suggestion API.

    Falls back to returning the IMDb ID as the query if the title can't be found.

    Raises:
        ImdbLookupError: if no IMDb ID is present or the lookup request fails.
    """
    imdb_match = re.search(r'tt\d+', query_text)
    if not imdb_match:
        raise ImdbLookupError("Could not extract IMDb ID from URL.")

    imdb_id = imdb_match.group(0)
    title = None
    imdb_year = None

    try:
        # Use IMDB's suggestion API to look up title by ID
        # This is more reliable than scraping the HTML page (which returns 202 for bots)
        suggestion_url = f"https://v3.sg.media-imdb.com/suggestion/t/{imdb_id}.json"
        response = requests.get(suggestion_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        }, timeout=10)
        response.raise_for_status()
        suggestion_data = response.json()
    except requests.RequestException as e:
        logger.error(f"Error fetching IMDb suggestion API: {e}")
        raise ImdbLookupError("Could not fetch IMDb data. Please try searching with the title directly.")

    for entry in suggestion_data.get('d', []):
        if entry.get('id') == imdb_id:
            title = entry.get('l')
            year = entry.get('y')
            if year:
                imdb_year = int(year)
            logger.debug(f"Extracted title '{title}' year={imdb_year} from IMDB suggestion API")
            break

    if title:
        return (title, imdb_year)

    # Fallback: use IMDb ID (might not work, but better than nothing)
    logger.warning(f"Could not extract title from IMDb suggestion API, using ID: {imdb_id}")
    return (imdb_id, None)


def clean_query_text(query_text: str) -> str:
    """Clean up query text - remove rating/metadata noise and extra whitespace."""
    query_text = query_text.strip()
    query_text = re.sub(r'\s*[⭐★]\s*\d+\.\d+.*$', '', query_text)  # Remove rating and after
    query_text = re.sub(r'\s*\|\s*.*$', '', query_text)  # Remove anything after |
    return query_text.strip()


def extract_search_year(query_text: str, item_type: str, imdb_year: Optional[int] = None) -> tuple[str, Optional[int]]:
    """Extract a year filter from the query text.

    Returns (query_text, search_year). Year filtering only applies to movies
    (Ombi's TV search API doesn't support it). A query that is *only* a year
    (e.g. the movie "1942") is left untouched.
    """
    search_year = None
    year_match = re.search(r'\b(19|20)\d{2}\b', query_text)
    if year_match:
        year = year_match.group(0)
        # Check if the entire query is just the year (possibly with whitespace)
        query_without_whitespace = query_text.strip()
        if query_without_whitespace == year or query_without_whitespace == f"({year})":
            # Entire query is just a year - don't extract it (could be a movie title like "1942")
            logger.debug(f"Query is just a year '{year}', keeping it as-is")
        else:
            # Query has text + year - remove year from query text
            query_text = re.sub(r'\s*[,\s]*\(?\s*\b(19|20)\d{2}\b\s*\)?\s*', ' ', query_text).strip()
            query_text = ' '.join(query_text.split())  # Normalize whitespace
            # Only use year for filtering if searching for movies
            if item_type == 'movie':
                search_year = int(year)
                logger.debug(f"Searching movie with year filter: '{query_text}' year={search_year}")
            else:
                logger.debug(f"Removed year from TV query: '{query_text}' (TV search doesn't support year filter)")
    elif imdb_year and item_type == 'movie':
        # Year was extracted from IMDB page (already removed from title)
        search_year = imdb_year
        logger.debug(f"Using year {search_year} from IMDB page for movie search")

    return (query_text, search_year)


def _search_tv_v2(query_text: str) -> Optional[list]:
    """Search TV shows via Ombi's v2 API, returning fully-detailed items.

    The v2 detail endpoint is the only one that reports Sonarr-cache
    availability (content that exists in Sonarr without an Ombi request), and
    it also carries theTvDbId (for requesting), imdbId and seasonRequests, so
    no further enrichment calls are needed.

    Returns None when v2 yields nothing usable so the caller can fall back to
    the legacy v1 search.
    """
    stubs = ombi_client.search_tv_v2(query_text)
    if not stubs:
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        details = list(pool.map(lambda s: ombi_client.get_tv_info_v2(s.get('id')), stubs))

    results = []
    for stub, detail in zip(stubs, details):
        # Without details there is no theTvDbId, and requesting with the wrong
        # ID type would request a different show - drop the result instead
        if detail and detail.get('theTvDbId'):
            results.append(detail)
        else:
            logger.warning(f"Dropping TV result without v2 details/TVDB id: '{stub.get('title')}'")

    _merge_tv_request_state(results)
    return results or None


def _merge_tv_request_state(results: list) -> None:
    """Stamp Ombi request state (denied/approved/requested) onto search results.

    The v2 detail endpoint reports Sonarr-cache availability but not request
    state - a requested/approved/denied show still comes back with requestId=0
    and denied=null. The authoritative record is the TV request list, keyed by
    tvDbId, with the flags on each childRequest. Without this merge a denied
    show looks freely requestable.
    """
    if not results:
        return
    try:
        requests_list = ombi_client.get_tv_requests()
    except Exception as e:
        logger.warning(f"Could not fetch TV requests to merge denial state: {e}")
        return

    by_tvdb = {}
    for req in requests_list:
        tvdb = str(req.get('tvDbId') or '')
        if tvdb:
            by_tvdb[tvdb] = req

    for item in results:
        req = by_tvdb.get(str(item.get('theTvDbId') or ''))
        if not req:
            continue
        children = req.get('childRequests') or []
        # denied beats everything (matches get_item_status priority); a show is
        # only requestable again once the denial is cleared in Ombi.
        if any(c.get('denied') for c in children):
            item['denied'] = True
            logger.debug(f"'{item.get('title')}' has a denied request - marking denied")
        elif any(c.get('approved') for c in children):
            item['approved'] = True
            item['requestId'] = req.get('id') or item.get('requestId')
        elif children:
            item['requested'] = True
            item['requestId'] = req.get('id') or item.get('requestId')


def perform_search(item_type: str, query_text: str, search_year: Optional[int] = None) -> list:
    """Search Ombi and post-process the results.

    Handles the no-results-with-year fallback, TV first-result detail
    enrichment and popularity sorting. Returns the (possibly empty) result list.
    """
    if not ombi_client:
        raise RuntimeError("Ombi client is not configured")

    # Search Ombi (movies support year filtering, TV does not)
    if item_type == 'movie':
        logger.info(f"Searching for movie with query: '{query_text}' year={search_year}")
        results = ombi_client.search_movie(query_text, year=search_year)
        # Fallback: if year search returned no results, retry without year filter
        if not results and search_year:
            logger.info(f"No results for '{query_text}' with year {search_year}, retrying without year filter")
            results = ombi_client.search_movie(query_text)
    else:
        logger.info(f"Searching for TV show with query: '{query_text}'")
        v2_results = _search_tv_v2(query_text)
        if v2_results is not None:
            logger.info(f"v2 TV search returned {len(v2_results)} detailed results for query: '{query_text}'")
            return v2_results
        logger.info("v2 TV search yielded nothing, falling back to v1 search")
        results = ombi_client.search_tv(query_text)

    logger.info(f"Search returned {len(results) if results else 0} results for query: '{query_text}'")

    if not results:
        return []

    # For TV shows (legacy v1 fallback path), get detailed info for the first
    # result which may have more accurate availability status
    if item_type == 'tv':
        first_item = results[0]
        # Try multiple ID fields - Ombi might use TMDB ID for the info endpoint
        tv_id = (first_item.get('theTvDbId') or first_item.get('tvDbId') or
                 first_item.get('tvdbId') or first_item.get('theMovieDbId') or  # Try TMDB ID as fallback
                 first_item.get('seriesId'))

        if tv_id:
            logger.debug(f"Fetching detailed TV info for ID: {tv_id}")
            detailed_info = ombi_client.get_tv_info(tv_id)
            if detailed_info:
                # Check seasonRequests in detailed info - might have availability per season
                if 'seasonRequests' in detailed_info:
                    season_requests = detailed_info.get('seasonRequests', [])
                    # Check if any seasons are available (partial availability)
                    if isinstance(season_requests, list) and len(season_requests) > 0:
                        available_seasons = [s for s in season_requests if s.get('available', False) or s.get('fullyAvailable', False)]
                        total_seasons = len(season_requests)
                        if available_seasons and len(available_seasons) < total_seasons:
                            logger.debug(f"Found {len(available_seasons)}/{total_seasons} seasons available - marking as partially available")
                            # Force partlyAvailable to True if we find some (but not all) seasons available
                            detailed_info['partlyAvailable'] = True
                # Merge detailed info into first result to get accurate availability status
                first_item.update(detailed_info)
                logger.debug(f"After merge - partlyAvailable: {first_item.get('partlyAvailable')}, available: {first_item.get('available')}, approved: {first_item.get('approved')}, requestId: {first_item.get('requestId')}")
            else:
                logger.warning(f"Failed to fetch detailed info for TV ID {tv_id} - trying alternative ID")
                # Try with TMDB ID if we used TVDb ID
                if tv_id != first_item.get('theMovieDbId') and first_item.get('theMovieDbId'):
                    logger.debug(f"Retrying with TMDB ID: {first_item.get('theMovieDbId')}")
                    detailed_info = ombi_client.get_tv_info(first_item.get('theMovieDbId'))
                    if detailed_info:
                        logger.debug("Successfully fetched detailed info with TMDB ID")
                        first_item.update(detailed_info)

    # Sort results by popularity (vote count, highest first)
    results.sort(key=lambda x: get_item_popularity(x, item_type), reverse=True)
    return results


def search_from_raw_query(item_type: str, raw_query: str) -> list:
    """Full search pipeline from a raw user query (title, title+year, or IMDb link).

    Used by the mini app; the bot calls the individual steps so it can send
    progress messages in between.

    Raises:
        ImdbLookupError: if an IMDb link is provided but can't be resolved.
        RuntimeError: if the Ombi client is not configured.
    """
    query_text = raw_query.strip()
    imdb_year = None

    if 'imdb.com' in query_text.lower():
        query_text, imdb_year = resolve_imdb_link(query_text)

    query_text = clean_query_text(query_text)
    query_text, search_year = extract_search_year(query_text, item_type, imdb_year)

    return perform_search(item_type, query_text, search_year)


def ensure_tv_seasons(item: dict) -> None:
    """Fetch and attach seasonRequests for a TV item if missing (needed for auto-approve)."""
    if item.get('seasonRequests'):
        return
    tv_id = (item.get('theTvDbId') or item.get('tvDbId') or item.get('tvdbId') or
             item.get('theMovieDbId') or item.get('id'))
    if tv_id and ombi_client:
        detailed_info = ombi_client.get_tv_info(tv_id)
        if detailed_info and detailed_info.get('seasonRequests'):
            item['seasonRequests'] = detailed_info['seasonRequests']
            logger.info(f"Fetched season info: {len(detailed_info['seasonRequests'])} seasons")
        else:
            logger.warning(f"Could not fetch season info for TV show (tv_id: {tv_id})")
    else:
        logger.warning(f"Cannot fetch season info: tv_id={tv_id}, ombi_client={'available' if ombi_client else 'None'}")


def should_auto_approve(item: dict, item_type: str) -> tuple[bool, str]:
    """Check if request should be auto-approved.

    Returns:
        tuple: (should_auto_approve, reason)
    """
    title = item.get('title') or item.get('Title') or item.get('name') or 'Unknown'
    logger.info(f"Checking auto-approve for {item_type}: {title}")

    # For TV shows, skip auto-approve if more than 3 seasons or if season count is unknown
    if item_type == 'tv':
        season_requests = item.get('seasonRequests')
        if not isinstance(season_requests, list) or len(season_requests) == 0:
            logger.info("Skipping auto-approve: TV show season count unknown (seasonRequests missing or empty)")
            return (False, "")
        if len(season_requests) > 3:
            logger.info(f"Skipping auto-approve: TV show has {len(season_requests)} seasons (>3)")
            return (False, "")

    # Check if year is current year (new releases)
    current_year = datetime.now().year
    year = get_item_year(item, item_type)
    logger.debug(f"Item year: {year}, current year: {current_year}")
    if year == current_year:
        logger.info(f"Auto-approve triggered: year is {current_year}")
        return (True, f"year is {current_year}")

    # Check if item exists in NZB
    if not nzb_client.enabled:
        logger.debug("NZB client not enabled, skipping NZB check")
        return (False, "")

    # Get title for search
    if item_type == 'movie':
        title = (item.get('title') or item.get('Title') or
                 item.get('movieTitle') or item.get('name') or '')
        imdb_id = item.get('imdbId') or item.get('imdbid')
        if nzb_client.search_movie(title, imdb_id):
            return (True, "found in NZB")
    else:
        title = (item.get('title') or item.get('Title') or
                 item.get('name') or item.get('Name') or
                 item.get('seriesName') or '')
        # Use tvdbId for more deterministic search
        tvdb_id = (item.get('theTvDbId') or item.get('tvDbId') or
                   item.get('tvdbId'))
        if nzb_client.search_tv(title, tvdb_id):
            return (True, "found in NZB")

    return (False, "")


def get_tmdb_id(item: Optional[dict]):
    """Extract the TMDB ID from an item, if it carries one.

    v2 search results set theMovieDbId (as a string); v1/TVMaze-backed results
    generally don't, so this returns None for them.
    """
    if not item:
        return None
    return item.get('theMovieDbId') or item.get('tmdbId') or item.get('tmdb_id')


def _submit_tv_request(item_id, item: Optional[dict], user_override: Optional[str]) -> bool:
    """Submit a TV request, preferring Ombi's TMDB-based v2 endpoint.

    Ombi's v1 /Request/tv looks the show up in TVMaze by TVDB id and 500s with
    "Object reference not set to an instance of an object" when TVMaze has no
    TVDB mapping for it (brand-new shows). Requesting by TMDB id via v2 avoids
    TVMaze entirely; fall back to v1 when we have no TMDB id.
    """
    tmdb_id = get_tmdb_id(item)
    if tmdb_id:
        if ombi_client.request_tv_v2(tmdb_id, user_override):
            return True
        logger.warning(f"v2 TV request failed for TMDB id {tmdb_id}, falling back to v1 with ID {item_id}")
    return ombi_client.request_tv(item_id, user_override)


def submit_request(item_type: str, item_id, item: Optional[dict] = None) -> tuple[bool, bool]:
    """Submit a request to Ombi, applying the auto-approve rules.

    Args:
        item_type: 'movie' or 'tv'
        item_id: the Ombi/TMDB/TVDB ID to request
        item: the raw search-result item if available (used for auto-approve
              checks); if None it is fetched from Ombi.

    Returns:
        tuple: (success, auto_approved)
    """
    if not ombi_client:
        raise RuntimeError("Ombi client is not configured")

    # If the caller doesn't have the raw item (e.g. stale mini app session),
    # fetch details so the auto-approve checks still work
    if item is None:
        if item_type == 'movie':
            item = ombi_client.get_movie_info(item_id)
        else:
            item = ombi_client.get_tv_info(item_id)
        if not item:
            logger.warning(f"Could not fetch {item_type} info for ID {item_id}; requesting without auto-approve check")

    auto_approve = False
    if item:
        # For TV shows, ensure we have season info for auto-approve check
        if item_type == 'tv':
            ensure_tv_seasons(item)
        auto_approve, auto_reason = should_auto_approve(item, item_type)
        if auto_approve:
            logger.info(f"Auto-approving {item_type} request (ID: {item_id}): {auto_reason}")

    user_override = OMBI_AUTO_APPROVE_USER if auto_approve else None

    if item_type == 'movie':
        success = ombi_client.request_movie(item_id, user_override)
    else:
        success = _submit_tv_request(item_id, item, user_override)

    if success and item is not None:
        # Mark as requested so cached result lists reflect the new status
        item['requested'] = True

    return (success, auto_approve)


def fetch_item(item_type: str, item_id) -> Optional[dict]:
    """Fetch the raw Ombi item for an ID (used when a mini app session expired)."""
    if not ombi_client:
        return None
    if item_type == 'movie':
        return ombi_client.get_movie_info(item_id)
    return ombi_client.get_tv_info(item_id)


# ---------- live request state (backs the mini app's "Requests" tab) ----------

# Ombi's request lists are the only place a submitted request's current state
# lives. They're fetched whole (one call per media type) and cached briefly so
# a history view with many entries doesn't hammer Ombi.
REQUEST_STATE_TTL_SECONDS = int(os.getenv('REQUEST_STATE_TTL_SECONDS', '60'))
_request_state_cache: dict = {}  # item_type -> (fetched_at, {item_id: state})


def _movie_request_state(req: dict) -> dict:
    """Derive (status, expectedDate) from an Ombi movie request record."""
    if req.get('denied'):
        status = 'denied'
    elif req.get('available'):
        status = 'available'
    elif req.get('approved'):
        status = 'approved'
    else:
        status = 'requested'
    expected = None if status in ('available', 'denied') else _upcoming_date(req.get('digitalReleaseDate'))
    return {'status': status, 'expectedDate': expected}


def _tv_request_state(req: dict) -> dict:
    """Derive (status, expectedDate) from an Ombi TV request record.

    Availability is per episode, so a show counts as available only once every
    requested episode has a file; anything in between is partially available.
    """
    children = req.get('childRequests') or []
    # Denied beats everything, matching get_item_status's priority
    if any(c.get('denied') for c in children):
        return {'status': 'denied', 'expectedDate': None}

    episodes = [
        ep
        for child in children
        for season in child.get('seasonRequests') or []
        for ep in season.get('episodes') or []
    ]
    if episodes:
        available = sum(1 for ep in episodes if ep.get('available'))
        if available == len(episodes):
            return {'status': 'available', 'expectedDate': None}
        if available:
            return {'status': 'partially_available', 'expectedDate': None}

    if any(c.get('approved') for c in children):
        return {'status': 'approved', 'expectedDate': None}
    return {'status': 'requested', 'expectedDate': None}


def get_request_state_map(item_type: str) -> dict:
    """Map of request ID -> current state for every request of a media type.

    Movies are keyed by TMDB id and TV shows by TVDB id, which is what
    get_item_id returns for each (and therefore what history rows store).
    Returns an empty map when Ombi is unreachable, so callers fall back to the
    status recorded at request time.
    """
    if not ombi_client:
        return {}

    cached = _request_state_cache.get(item_type)
    if cached and time.time() - cached[0] < REQUEST_STATE_TTL_SECONDS:
        return cached[1]

    try:
        if item_type == 'movie':
            requests_list = ombi_client.get_movie_requests()
            states = {
                str(req.get('theMovieDbId')): _movie_request_state(req)
                for req in requests_list if req.get('theMovieDbId')
            }
        else:
            requests_list = ombi_client.get_tv_requests()
            states = {
                str(req.get('tvDbId')): _tv_request_state(req)
                for req in requests_list if req.get('tvDbId')
            }
    except Exception as e:
        logger.warning(f"Could not fetch {item_type} request state from Ombi: {e}")
        return {}

    logger.debug(f"Fetched state for {len(states)} {item_type} requests")
    _request_state_cache[item_type] = (time.time(), states)
    return states


def refresh_request_statuses(entries: list) -> list:
    """Update stored history entries in place with their current Ombi status.

    Entries Ombi no longer knows about (request deleted, or made under a
    different Ombi user) keep the status recorded when they were requested.
    """
    needed = {entry['type'] for entry in entries if entry.get('type')}
    state_maps = {item_type: get_request_state_map(item_type) for item_type in needed}

    for entry in entries:
        state = state_maps.get(entry.get('type'), {}).get(str(entry.get('id')))
        if state:
            entry['status'] = state['status']
            entry['expectedDate'] = state['expectedDate']
        else:
            entry.setdefault('expectedDate', None)
    return entries


def enrich_movie_imdb(item: dict) -> None:
    """Fetch detailed movie info to attach the IMDB ID and digital release date.

    Movie search results carry neither: they have no imdbId, and their
    digitalReleaseDate is always null. Only the detail endpoint fills both in.
    """
    if not ombi_client or item.get('_detailsFetched'):
        return
    movie_db_id = item.get('theMovieDbId') or item.get('id')
    if not movie_db_id:
        return
    movie_info = ombi_client.get_movie_info(movie_db_id)
    if not movie_info:
        return
    item['_detailsFetched'] = True
    if movie_info.get('imdbId'):
        item['imdbId'] = movie_info['imdbId']
        logger.debug(f"Fetched IMDB ID for movie: {movie_info['imdbId']}")
    if movie_info.get('digitalReleaseDate'):
        item['digitalReleaseDate'] = movie_info['digitalReleaseDate']
        logger.debug(f"Fetched digital release date: {movie_info['digitalReleaseDate']}")


def enrich_item_imdb(item: dict, item_type: str) -> None:
    """Attach the IMDB ID to a movie or TV item (no-op if already present).

    Search results don't include IMDB IDs; only the detail endpoints do. Safe to
    call on every result (it short-circuits when the ID is already known).
    """
    if not ombi_client:
        return
    if item_type == 'movie':
        enrich_movie_imdb(item)
        return
    if item.get('imdbId') or item.get('imdbid'):
        return
    tv_id = (item.get('theTvDbId') or item.get('tvDbId') or item.get('tvdbId') or
             item.get('theMovieDbId') or item.get('id'))
    if tv_id:
        info = ombi_client.get_tv_info(tv_id)
        if info and info.get('imdbId'):
            item['imdbId'] = info['imdbId']
            logger.debug(f"Fetched IMDB ID for TV show: {info['imdbId']}")


def normalize_item(item: dict, item_type: str) -> dict:
    """Convert a raw Ombi item into the JSON shape the mini app consumes."""
    should_hide, status = get_item_status(item)
    imdb_id = item.get('imdbId') or item.get('imdbid')
    rating = get_item_rating(item, item_type)
    # Pointless for anything already in the library; computed for requestable
    # items too, so the mini app can show a date the moment a request is approved
    expected_date = (None if status in ('available', 'partially_available', 'denied')
                     else get_expected_date(item, item_type))
    return {
        'id': get_item_id(item),
        'type': item_type,
        'title': get_item_title(item, item_type),
        'year': get_item_year(item, item_type) or None,
        'rating': round(rating, 1) if rating > 0 else None,
        'overview': get_item_overview(item),
        'poster': get_poster_url(item),
        'imdbId': imdb_id,
        'imdbUrl': f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else None,
        'status': status,
        'expectedDate': expected_date,
        'canRequest': not should_hide,
    }
