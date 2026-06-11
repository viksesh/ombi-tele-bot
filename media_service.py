"""Shared media search/request logic used by both the Telegram bot and the mini app.

This module owns the Ombi/NZB clients and all surface-agnostic business logic:
IMDb link resolution, query parsing, searching, status detection, auto-approve
rules and request submission. bot.py (messaging) and webapp_server.py (mini app)
are thin presentation layers on top of it.
"""

import os
import re
import logging
import requests
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


def get_item_status(item: dict):
    """Check item status and return (should_hide_request, status_text).

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
        results = ombi_client.search_tv(query_text)

    logger.info(f"Search returned {len(results) if results else 0} results for query: '{query_text}'")

    if not results:
        return []

    # For TV shows, get detailed info for the first result which may have more
    # accurate availability status
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
        success = ombi_client.request_tv(item_id, user_override)

    if success and item is not None:
        # Mark as requested so cached result lists reflect the new status
        item['requested'] = True

    return (success, auto_approve)


def enrich_movie_imdb(item: dict) -> None:
    """Fetch detailed movie info to attach the IMDB ID (search results don't include it)."""
    if not ombi_client or item.get('imdbId') or item.get('imdbid'):
        return
    movie_db_id = item.get('theMovieDbId') or item.get('id')
    if movie_db_id:
        movie_info = ombi_client.get_movie_info(movie_db_id)
        if movie_info and movie_info.get('imdbId'):
            item['imdbId'] = movie_info['imdbId']
            logger.debug(f"Fetched IMDB ID for movie: {movie_info['imdbId']}")


def normalize_item(item: dict, item_type: str) -> dict:
    """Convert a raw Ombi item into the JSON shape the mini app consumes."""
    should_hide, status = get_item_status(item)
    imdb_id = item.get('imdbId') or item.get('imdbid')
    rating = get_item_rating(item, item_type)
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
        'canRequest': not should_hide,
    }
