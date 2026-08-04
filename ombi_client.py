import os
import requests
import logging
from typing import List, Dict, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)


class OmbiClient:
    """Client for interacting with Ombi API."""
    
    def __init__(self):
        base_url_raw = os.getenv('OMBI_URL', '').strip()
        # Strip quotes if present (common when setting env vars in Docker/shell scripts)
        self.base_url = base_url_raw.strip('"\' ').rstrip('/')
        api_key = os.getenv('OMBI_API_KEY', '')
        # Strip quotes if present (common when setting env vars in Docker/shell scripts)
        self.api_key = api_key.strip('"\' ') if api_key else ''
        self.request_user = os.getenv('OMBI_REQUEST_USER', '').strip()  # Username to make requests on behalf of
        
        # Log request user configuration
        if self.request_user:
            logger.info(f"OmbiClient initialized with request user: '{self.request_user}'")
        else:
            logger.debug("OmbiClient initialized without request user (requests will use API key user)")
        
        if not self.base_url:
            raise ValueError("OMBI_URL environment variable is required")
        if not self.api_key:
            raise ValueError("OMBI_API_KEY environment variable is required")
        
        logger.debug(f"OmbiClient initialized with base_url: '{self.base_url}'")
        
        self.headers = {
            'ApiKey': self.api_key,
            'Content-Type': 'application/json'
        }
        
        # Add UserName header if request_user is specified
        if self.request_user:
            self.headers['UserName'] = self.request_user
    
    def _make_request(self, method: str, endpoint: str, headers: Dict = None, api_version: int = 1, **kwargs) -> Optional[Dict]:
        """Make a request to the Ombi API."""
        url = f"{self.base_url}/api/v{api_version}{endpoint}"
        request_headers = headers if headers else self.headers

        try:
            logger.debug(f"Making {method} request to: {url}")
            response = requests.request(method, url, headers=request_headers, **kwargs)
            response.raise_for_status()
            
            if response.content:
                result = response.json()
                logger.debug(f"API response type: {type(result)}, content preview: {str(result)[:200]}")
                return result
            logger.debug("API returned empty response")
            return {}
        except requests.exceptions.RequestException as e:
            logger.error(f"Ombi API error for {method} {url}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response status: {e.response.status_code}, body: {e.response.text[:500]}")
            return None
    
    def search_movie(self, query: str, year: int = None) -> List[Dict]:
        """Search for movies in Ombi.

        Args:
            query: Search query (movie name)
            year: Optional year to filter results (passed to TMDB for server-side filtering)

        Returns:
            List of movie dictionaries with id, title, year, etc.
        """
        logger.info(f"Searching for movie: '{query}' year={year}")

        # Use POST endpoint with JSON body when year is provided (supports refined search)
        # Use GET endpoint otherwise (simpler, well-tested path)
        if year:
            endpoint = "/Search/movie"
            data = {"searchTerm": query, "year": year}
            result = self._make_request('POST', endpoint, json=data)
        else:
            encoded_query = quote(query)
            endpoint = f"/Search/movie/{encoded_query}"
            result = self._make_request('GET', endpoint)
        
        if result is None:
            logger.warning(f"Movie search returned None for query: '{query}'")
            return []
        
        # Ombi API returns results in different formats, handle both
        if isinstance(result, list):
            logger.info(f"Movie search returned {len(result)} results (list format)")
            return result
        elif isinstance(result, dict):
            if 'results' in result:
                results_list = result['results']
                logger.info(f"Movie search returned {len(results_list) if isinstance(results_list, list) else 'non-list'} results (dict with 'results' key)")
                return results_list if isinstance(results_list, list) else []
            elif 'data' in result:
                results_list = result['data']
                logger.info(f"Movie search returned {len(results_list) if isinstance(results_list, list) else 'non-list'} results (dict with 'data' key)")
                return results_list if isinstance(results_list, list) else []
            else:
                logger.warning(f"Movie search returned dict but no 'results' or 'data' key. Keys: {list(result.keys())}, full response: {str(result)[:500]}")
                return []
        else:
            logger.warning(f"Movie search returned unexpected type: {type(result)}, value: {str(result)[:500]}")
            return []
    
    def search_tv(self, query: str) -> List[Dict]:
        """Search for TV shows in Ombi.

        Args:
            query: Search query (TV show name)

        Returns:
            List of TV show dictionaries with id, title, year, etc.
        """
        # Note: Unlike movies, Ombi's TV search API doesn't support year filtering
        encoded_query = quote(query)
        endpoint = f"/Search/tv/{encoded_query}"
        logger.info(f"Searching for TV show: '{query}' (encoded: '{encoded_query}')")
        result = self._make_request('GET', endpoint)

        if result is None:
            logger.warning(f"TV search returned None for query: '{query}'")
            return []

        # Ombi API returns results in different formats, handle both
        if isinstance(result, list):
            logger.info(f"TV search returned {len(result)} results (list format)")
            return result
        elif isinstance(result, dict):
            if 'results' in result:
                results_list = result['results']
                logger.info(f"TV search returned {len(results_list) if isinstance(results_list, list) else 'non-list'} results (dict with 'results' key)")
                return results_list if isinstance(results_list, list) else []
            elif 'data' in result:
                results_list = result['data']
                logger.info(f"TV search returned {len(results_list) if isinstance(results_list, list) else 'non-list'} results (dict with 'data' key)")
                return results_list if isinstance(results_list, list) else []
            else:
                logger.warning(f"TV search returned dict but no 'results' or 'data' key. Keys: {list(result.keys())}, full response: {str(result)[:500]}")
                return []
        else:
            logger.warning(f"TV search returned unexpected type: {type(result)}, value: {str(result)[:500]}")
            return []
    
    def search_tv_v2(self, query: str) -> List[Dict]:
        """Search for TV shows via Ombi's v2 multi-search (TMDB-backed).

        The v1 TV search is TVMaze-based and returns items with a null
        theTvDbId, which makes Ombi skip its Sonarr-cache availability rules —
        shows that already exist in Sonarr look requestable. The v2 endpoints
        (the ones Ombi's own web UI uses) report that availability correctly.

        Returns:
            List of lightweight result stubs: id (TMDB), title, poster, overview.
        """
        encoded_query = quote(query)
        endpoint = f"/search/multi/{encoded_query}"
        data = {"movies": False, "tvShows": True, "music": False, "people": False}
        result = self._make_request('POST', endpoint, json=data, api_version=2)

        if not isinstance(result, list):
            if result is not None:
                logger.warning(f"v2 TV search returned unexpected type: {type(result)}")
            return []
        tv_results = [r for r in result if r.get('mediaType') == 'tv']
        logger.info(f"v2 TV search returned {len(tv_results)} results for '{query}'")
        return tv_results

    def get_tv_info_v2(self, tmdb_id) -> Optional[Dict]:
        """Get detailed TV show information via the v2 API.

        Unlike the v1 info endpoint, this includes availability computed from
        the Sonarr cache (show-level 'available' plus per-episode 'approved'),
        and carries theTvDbId needed for submitting requests.

        Args:
            tmdb_id: The TMDB ID of the show (v2 search result 'id')

        Returns:
            TV show dictionary, or None on error/no content
        """
        endpoint = f"/search/tv/moviedb/{tmdb_id}"
        result = self._make_request('GET', endpoint, api_version=2)
        # Ombi returns 204 (empty body) when it can't resolve the show
        return result or None

    def get_tv_requests(self) -> List[Dict]:
        """Fetch all TV requests from Ombi.

        The search/detail endpoints don't carry request state - a show that was
        requested, approved or denied still comes back with requestId=0 and
        denied=null. Only the request list records that, keyed by tvDbId, with
        the denied/approved flags living on each childRequest.
        """
        result = self._make_request('GET', '/Request/tv')
        return result if isinstance(result, list) else []

    def request_movie(self, movie_id: int, user_override: str = None) -> bool:
        """Request a movie in Ombi.

        Args:
            movie_id: The ID of the movie to request
            user_override: Optional username to override the default request user

        Returns:
            True if request was successful, False otherwise
        """
        endpoint = "/Request/movie"
        data = {"theMovieDbId": movie_id}

        # Determine which user to use
        request_user = user_override or self.request_user

        # Username is passed via UserName header
        if request_user:
            logger.info(f"Making request on behalf of user: {request_user} (via UserName header)")
        else:
            logger.warning("No request user set, request will be made with API key user")

        logger.debug(f"Request payload: {data}")

        # Use custom headers if user_override is provided
        if user_override:
            headers = self.headers.copy()
            headers['UserName'] = user_override
            result = self._make_request('POST', endpoint, json=data, headers=headers)
        else:
            result = self._make_request('POST', endpoint, json=data)
        return result is not None
    
    def request_tv(self, tv_id: int, user_override: str = None) -> bool:
        """Request a TV show in Ombi.

        Args:
            tv_id: The ID of the TV show to request
            user_override: Optional username to override the default request user

        Returns:
            True if request was successful, False otherwise
        """
        endpoint = "/Request/tv"
        # Include requestAll: true to request all seasons by default
        # Without this, Ombi may only request the first season or not create a request
        # for multi-season shows, which can cause requests to not show up in pending
        data = {"tvDbId": tv_id, "requestAll": True}

        # Determine which user to use
        request_user = user_override or self.request_user

        # Username is passed via UserName header
        if request_user:
            logger.info(f"Making request on behalf of user: {request_user} (via UserName header)")
        else:
            logger.warning("No request user set, request will be made with API key user")

        logger.debug(f"Request payload: {data}")

        # Use custom headers if user_override is provided
        if user_override:
            headers = self.headers.copy()
            headers['UserName'] = user_override
            result = self._make_request('POST', endpoint, json=data, headers=headers)
        else:
            result = self._make_request('POST', endpoint, json=data)
        return result is not None
    
    def request_tv_v2(self, tmdb_id, user_override: str = None) -> bool:
        """Request a TV show via Ombi's v2 (TheMovieDb-based) request endpoint.

        The v1 /Request/tv endpoint resolves the show by looking it up in TVMaze
        by TVDB id. When TVMaze has no TVDB mapping for a show - common for
        brand-new titles - that lookup returns null and Ombi throws a
        NullReferenceException (HTTP 500). The v2 endpoint takes the TMDB id and
        resolves through TheMovieDb, which is where our search results already
        come from, so it isn't affected.

        Args:
            tmdb_id: The TMDB ID of the show
            user_override: Optional username to override the default request user

        Returns:
            True if request was successful, False otherwise
        """
        try:
            tmdb_id = int(tmdb_id)
        except (TypeError, ValueError):
            logger.warning(f"Cannot make v2 TV request with non-numeric TMDB id: {tmdb_id!r}")
            return False

        endpoint = "/Requests/tv"
        data = {"theMovieDbId": tmdb_id, "requestAll": True, "languageCode": "en"}

        request_user = user_override or self.request_user
        if request_user:
            logger.info(f"Making v2 TV request on behalf of user: {request_user} (via UserName header)")
        else:
            logger.warning("No request user set, request will be made with API key user")

        logger.debug(f"v2 TV request payload: {data}")

        headers = None
        if user_override:
            headers = self.headers.copy()
            headers['UserName'] = user_override
        result = self._make_request('POST', endpoint, json=data, headers=headers, api_version=2)
        return self._request_succeeded(result)

    @staticmethod
    def _request_succeeded(result) -> bool:
        """Check an Ombi RequestEngineResult body (2xx can still mean rejected)."""
        if result is None:
            return False
        if isinstance(result, dict) and result.get('result') is False:
            logger.error(f"Ombi rejected the request: {result.get('errorMessage')}")
            return False
        return True

    def get_movie_info(self, movie_id: int) -> Optional[Dict]:
        """Get detailed movie information from Ombi.

        This endpoint returns more data than search results, including imdbId.

        Args:
            movie_id: The MovieDb ID of the movie

        Returns:
            Movie dictionary with detailed information, or None if error
        """
        endpoint = f"/Search/movie/info/{movie_id}"
        return self._make_request('GET', endpoint)

    def get_tv_info(self, tv_id: int) -> Optional[Dict]:
        """Get detailed TV show information from Ombi.

        This endpoint may have more accurate availability status than search results.

        Args:
            tv_id: The TVDb ID of the TV show

        Returns:
            TV show dictionary with detailed information, or None if error
        """
        endpoint = f"/Search/tv/info/{tv_id}"
        return self._make_request('GET', endpoint)

