"""
Polygon.io API Base Client

Handles authentication, rate limiting, retries, and pagination.
"""

import requests
import time
from typing import Dict, List, Optional
from ratelimit import limits, sleep_and_retry
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PolygonAPIError(Exception):
    """Custom exception for Polygon.io API errors"""
    pass


class PolygonClient:
    """
    Base client for Polygon.io API with retry logic and rate limiting.
    
    Attributes:
        BASE_URL: Polygon.io API base URL
        api_key: Your Polygon.io API key
        session: Requests session for connection pooling
    """
    
    BASE_URL = "https://api.polygon.io"
    
    def __init__(self, api_key: str, rate_limit_calls: int = 5, rate_limit_period: int = 1):
        """
        Initialize Polygon.io API client.
        
        Args:
            api_key: Polygon.io API key
            rate_limit_calls: Number of API calls allowed per period
            rate_limit_period: Time period in seconds for rate limiting
        """
        if not api_key:
            raise ValueError("API key is required")
        
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "ChronoX-Trading-Bot/0.1.0"
        })
        self.rate_limit_calls = rate_limit_calls
        self.rate_limit_period = rate_limit_period
        
        logger.info("Polygon.io client initialized")
    
    @sleep_and_retry
    @limits(calls=5, period=1)
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type((requests.exceptions.RequestException, requests.exceptions.Timeout)),
        reraise=True
    )
    def _get(self, endpoint: str, params: Optional[Dict] = None) -> Dict:
        """
        Make GET request with retry logic and rate limiting.
        
        Args:
            endpoint: API endpoint path (e.g., '/v2/aggs/ticker/AAPL/range/...')
            params: Query parameters
        
        Returns:
            JSON response as dictionary
        
        Raises:
            PolygonAPIError: If API returns an error
            requests.exceptions.RequestException: For network errors
        """
        params = params or {}
        
        # API key can be passed as query param or header
        if 'apiKey' not in params:
            params['apiKey'] = self.api_key
        
        url = f"{self.BASE_URL}{endpoint}"
        
        try:
            logger.debug(f"GET {endpoint} with params: {params}")
            response = self.session.get(url, params=params, timeout=30)
            
            # Handle rate limiting
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 60))
                logger.warning(f"Rate limited. Waiting {retry_after} seconds...")
                time.sleep(retry_after)
                return self._get(endpoint, params)
            
            response.raise_for_status()
            data = response.json()
            
            # Check for API-level errors
            if data.get('status') == 'ERROR':
                error_msg = data.get('error', 'Unknown API error')
                logger.error(f"API Error: {error_msg}")
                raise PolygonAPIError(error_msg)
            
            return data
            
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP Error {e.response.status_code}: {e.response.text}")
            raise
        except requests.exceptions.Timeout:
            logger.error(f"Request timeout for {endpoint}")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed: {str(e)}")
            raise
    
    def get_with_pagination(self, endpoint: str, params: Optional[Dict] = None, 
                           max_results: Optional[int] = None) -> List[Dict]:
        """
        Handle cursor-based pagination automatically.
        
        Args:
            endpoint: API endpoint path
            params: Query parameters
            max_results: Maximum number of results to return (None = all)
        
        Returns:
            List of all results across all pages
        """
        all_results = []
        params = params or {}
        collected = 0
        
        while True:
            data = self._get(endpoint, params)
            results = data.get('results', [])
            
            if not results:
                break
            
            all_results.extend(results)
            collected += len(results)
            
            logger.info(f"Fetched {len(results)} records (total: {collected})")
            
            # Check if we've hit the max results limit
            if max_results and collected >= max_results:
                all_results = all_results[:max_results]
                break
            
            # Check for next page
            next_url = data.get('next_url')
            if not next_url:
                break
            
            # Extract cursor from next_url
            if 'cursor=' in next_url:
                cursor = next_url.split('cursor=')[1].split('&')[0]
                params['cursor'] = cursor
            else:
                break
        
        logger.info(f"Pagination complete. Total records: {len(all_results)}")
        return all_results
    
    def test_connection(self) -> bool:
        """
        Test API connection and authentication.
        
        Returns:
            True if connection successful, False otherwise
        """
        try:
            # Try to fetch a simple endpoint
            data = self._get("/v3/reference/tickers", {"limit": 1})
            logger.info("✓ API connection successful")
            return True
        except Exception as e:
            logger.error(f"✗ API connection failed: {str(e)}")
            return False
    
    def close(self):
        """Close the session"""
        self.session.close()
        logger.info("Session closed")
    
    def __enter__(self):
        """Context manager entry"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()


if __name__ == "__main__":
    # Test the client
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    api_key = os.getenv("POLYGON_API_KEY")
    
    if not api_key:
        print("ERROR: POLYGON_API_KEY not found in .env file")
        exit(1)
    
    # Test connection
    with PolygonClient(api_key) as client:
        if client.test_connection():
            print("\n✓ Polygon.io client is working correctly!")
            
            # Fetch a sample ticker
            print("\nFetching sample ticker data...")
            data = client._get("/v3/reference/tickers/AAPL")
            print(f"Sample: {data.get('results', {}).get('name', 'N/A')}")
        else:
            print("\n✗ Connection test failed. Check your API key.")
