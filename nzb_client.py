import os
import re
import requests
import logging
from typing import Dict, List, Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

# Pattern to match season packs (S01, S02, Season 1, Season.1, etc.)
SEASON_PACK_PATTERN = re.compile(
    r'\b(S\d{1,2}(?!E\d)|Season[\s._-]?\d{1,2})\b',
    re.IGNORECASE
)


class NZBClient:
    """Client for interacting with NZBFinder API v2."""

    def __init__(self):
        base_url_raw = os.getenv('NZB_URL', '').strip()
        self.base_url = base_url_raw.strip('"\' ').rstrip('/')
        api_key = os.getenv('NZB_API_KEY', '')
        self.api_key = api_key.strip('"\' ') if api_key else ''

        if not self.base_url or not self.api_key:
            logger.info("NZB_URL or NZB_API_KEY not configured - NZB auto-approve feature disabled")
            self.enabled = False
        else:
            self.enabled = True
            logger.info(f"NZBClient initialized with base_url: '{self.base_url}'")

    def _make_request(self, endpoint: str, params: Dict) -> Optional[Dict]:
        """Make a request to the NZBFinder v2 API.

        Args:
            endpoint: API endpoint path (e.g., 'movies', 'tv', 'search')
            params: Query parameters
        """
        if not self.enabled:
            return None

        params['api_token'] = self.api_key

        # Build v2 API URL - strip /api suffix if present
        base = self.base_url.rstrip('/')
        if base.endswith('/api'):
            base = base[:-4]
        url = f"{base}/api/v2/{endpoint}?{urlencode(params)}"

        try:
            logger.debug(f"Making NZB API request: {url.replace(self.api_key, '***')}")
            headers = {
                'Authorization': f'Bearer {self.api_key}',
                'User-Agent': 'NZBClient/1.0',
            }
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()

            if response.content:
                result = response.json()
                logger.debug(f"NZB API response total: {result.get('total', 0)}")
                return result
            logger.debug("NZB API returned empty response")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"NZB API error: {e}")
            return None
        except ValueError as e:
            logger.error(f"NZB API JSON parse error: {e}")
            return None

    def _get_items(self, response: Optional[Dict]) -> List[Dict]:
        """Extract items from NZB API v2 response."""
        if not response:
            return []

        items = response.get('results', [])
        if isinstance(items, list):
            return items
        if items:
            return [items]
        return []

    def _has_results(self, response: Optional[Dict]) -> bool:
        """Check if NZB API response contains results."""
        return len(self._get_items(response)) > 0

    def _has_season_pack(self, response: Optional[Dict]) -> bool:
        """Check if NZB API response contains season packs.

        Looks for results with season indicators like 'S01', 'Season 1', etc.
        but NOT individual episodes like 'S01E01'.
        """
        items = self._get_items(response)
        if not items:
            return False

        for item in items:
            title = item.get('title') or item.get('name') or ''

            if SEASON_PACK_PATTERN.search(title):
                logger.debug(f"Found season pack: {title}")
                return True

        logger.debug(f"No season packs found in {len(items)} results")
        return False

    def search_movie(self, title: str, imdb_id: Optional[str] = None) -> bool:
        """Search for a movie in NZB indexer.

        Args:
            title: Movie title for search
            imdb_id: Optional IMDb ID (e.g., 'tt1234567')

        Returns:
            True if movie exists in NZB indexer, False otherwise
        """
        if not self.enabled:
            return False

        # Use dedicated movies endpoint with IMDB ID if available
        if imdb_id:
            clean_imdb = imdb_id if imdb_id.startswith('tt') else f'tt{imdb_id}'
            params = {'imdbid': clean_imdb}
            logger.info(f"Searching NZB for movie by IMDB ID: {clean_imdb}")
            result = self._make_request('movies', params)
            if self._has_results(result):
                logger.info(f"Found movie in NZB by IMDB ID: {clean_imdb}")
                return True

        # Fall back to general search with movies category
        params = {'query': title, 'cat': '2000'}
        logger.info(f"Searching NZB for movie by title: {title}")
        result = self._make_request('search', params)
        found = self._has_results(result)
        logger.info(f"NZB movie search for '{title}': {'found' if found else 'not found'}")
        return found

    def search_tv(self, title: str, tvdb_id: Optional[int] = None) -> bool:
        """Search for a TV show season pack in NZB indexer.

        Only returns True if season packs are found (e.g., 'S01', 'Season 1'),
        not individual episodes (e.g., 'S01E01').

        Args:
            title: TV show title for search
            tvdb_id: Optional TVDB ID

        Returns:
            True if TV show season pack exists in NZB indexer, False otherwise
        """
        if not self.enabled:
            return False

        # Use dedicated TV endpoint with TVDB ID if available
        if tvdb_id:
            params = {'tvdbid': tvdb_id}
            logger.info(f"Searching NZB for TV show season packs by TVDB ID: {tvdb_id}")
            result = self._make_request('tv', params)
            if self._has_season_pack(result):
                logger.info(f"Found TV show season pack in NZB by TVDB ID: {tvdb_id}")
                return True

        # Fall back to general search with title
        params = {'query': title, 'cat': '5000'}
        logger.info(f"Searching NZB for TV show season packs by title: {title}")
        result = self._make_request('search', params)
        found = self._has_season_pack(result)
        logger.info(f"NZB TV season pack search for '{title}': {'found' if found else 'not found'}")
        return found
