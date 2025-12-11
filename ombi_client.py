import os
import requests
from typing import List, Dict, Optional
from urllib.parse import quote


class OmbiClient:
    """Client for interacting with Ombi API."""
    
    def __init__(self):
        self.base_url = os.getenv('OMBI_URL', '').rstrip('/')
        self.api_key = os.getenv('OMBI_API_KEY', '')
        self.request_user = os.getenv('OMBI_REQUEST_USER', '').strip()  # Username to make requests on behalf of
        
        # Log request user configuration (use logging instead of print)
        import logging
        logger = logging.getLogger(__name__)
        if self.request_user:
            logger.info(f"OmbiClient initialized with request user: '{self.request_user}'")
        else:
            logger.debug("OmbiClient initialized without request user (requests will use API key user)")
        
        if not self.base_url:
            raise ValueError("OMBI_URL environment variable is required")
        if not self.api_key:
            raise ValueError("OMBI_API_KEY environment variable is required")
        
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
            response = requests.request(method, url, headers=self.headers, **kwargs)
            response.raise_for_status()
            
            if response.content:
                return response.json()
            return {}
        except requests.exceptions.RequestException as e:
            print(f"Ombi API error: {e}")
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
        result = self._make_request('GET', endpoint)
        
        if result is None:
            return []
        
        # Ombi API returns results in different formats, handle both
        if isinstance(result, list):
            return result
        elif isinstance(result, dict) and 'results' in result:
            return result['results']
        elif isinstance(result, dict) and 'data' in result:
            return result['data']
        else:
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
        result = self._make_request('GET', endpoint)
        
        if result is None:
            return []
        
        # Ombi API returns results in different formats, handle both
        if isinstance(result, list):
            return result
        elif isinstance(result, dict) and 'results' in result:
            return result['results']
        elif isinstance(result, dict) and 'data' in result:
            return result['data']
        else:
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
            print(f"Making request on behalf of user: {self.request_user} (via UserName header)")
        else:
            print("WARNING: OMBI_REQUEST_USER not set, request will be made with API key user")
        
        print(f"Request payload: {data}")
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
            print(f"Making request on behalf of user: {self.request_user} (via UserName header)")
        else:
            print("WARNING: OMBI_REQUEST_USER not set, request will be made with API key user")
        
        print(f"Request payload: {data}")
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

