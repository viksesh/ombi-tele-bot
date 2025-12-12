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
    
    def _make_request(self, method: str, endpoint: str, **kwargs) -> Optional[Dict]:
        """Make a request to the Ombi API."""
        url = f"{self.base_url}/api/v1{endpoint}"
        
        try:
            logger.debug(f"Making {method} request to: {url}")
            response = requests.request(method, url, headers=self.headers, **kwargs)
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
    
    def search_movie(self, query: str) -> List[Dict]:
        """Search for movies in Ombi.
        
        Args:
            query: Search query (movie name, optionally with year)
            
        Returns:
            List of movie dictionaries with id, title, year, etc.
        """
        # URL encode the query to handle special characters
        encoded_query = quote(query)
        endpoint = f"/Search/movie/{encoded_query}"
        logger.info(f"Searching for movie: '{query}' (encoded: '{encoded_query}')")
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
            query: Search query (TV show name, optionally with year)
            
        Returns:
            List of TV show dictionaries with id, title, year, etc.
        """
        # URL encode the query to handle special characters
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
    
    def request_movie(self, movie_id: int) -> bool:
        """Request a movie in Ombi.
        
        Args:
            movie_id: The ID of the movie to request
        
        Returns:
            True if request was successful, False otherwise
        """
        endpoint = "/Request/movie"
        data = {"theMovieDbId": movie_id}
        
        # Username is passed via UserName header (set in __init__)
        if self.request_user:
            logger.info(f"Making request on behalf of user: {self.request_user} (via UserName header)")
        else:
            logger.warning("OMBI_REQUEST_USER not set, request will be made with API key user")
        
        logger.debug(f"Request payload: {data}")
        result = self._make_request('POST', endpoint, json=data)
        return result is not None
    
    def request_tv(self, tv_id: int) -> bool:
        """Request a TV show in Ombi.
        
        Args:
            tv_id: The ID of the TV show to request
        
        Returns:
            True if request was successful, False otherwise
        """
        endpoint = "/Request/tv"
        data = {"tvDbId": tv_id}
        
        # Username is passed via UserName header (set in __init__)
        if self.request_user:
            logger.info(f"Making request on behalf of user: {self.request_user} (via UserName header)")
        else:
            logger.warning("OMBI_REQUEST_USER not set, request will be made with API key user")
        
        logger.debug(f"Request payload: {data}")
        result = self._make_request('POST', endpoint, json=data)
        return result is not None
    
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

