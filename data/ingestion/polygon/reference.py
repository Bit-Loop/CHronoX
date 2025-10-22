"""
Polygon.io Reference Data API Client

Handles fetching ticker metadata, exchanges, and market information.
"""

from typing import List, Dict, Optional
import logging

logger = logging.getLogger(__name__)


class ReferenceClient:
    """
    Client for fetching reference data from Polygon.io.
    
    Includes ticker details, exchanges, market status, and conditions.
    """
    
    def __init__(self, polygon_client):
        """
        Initialize reference data client.
        
        Args:
            polygon_client: Instance of PolygonClient
        """
        self.client = polygon_client
    
    def get_ticker_details(self, ticker: str, date: Optional[str] = None) -> Optional[Dict]:
        """
        Get comprehensive metadata for a ticker.
        
        Args:
            ticker: Stock symbol (e.g., 'AAPL')
            date: Get details as of this date (YYYY-MM-DD). None = most recent.
        
        Returns:
            Dict with keys:
                - ticker: Symbol
                - name: Company name
                - market: Market type (stocks, crypto, fx)
                - locale: Locale (us, global)
                - primary_exchange: Primary listing exchange
                - type: Security type (CS=Common Stock, ETF, etc.)
                - active: Currently active/tradable
                - currency_name: Trading currency
                - cik: SEC CIK number
                - composite_figi: FIGI identifier
                - share_class_figi: Share class FIGI
                - market_cap: Market capitalization
                - phone_number: Company phone
                - address: Company address
                - description: Company description
                - sic_code: SIC industry code
                - sic_description: SIC industry description
                - ticker_root: Root symbol
                - homepage_url: Company website
                - total_employees: Employee count
                - list_date: IPO/listing date
                - branding: Logo and icon URLs
                - share_class_shares_outstanding: Shares outstanding
                - weighted_shares_outstanding: Weighted shares
        """
        endpoint = f"/v3/reference/tickers/{ticker}"
        params = {}
        
        if date:
            params['date'] = date
        
        try:
            data = self.client._get(endpoint, params)
            results = data.get('results', {})
            
            if results:
                logger.info(f"Fetched details for {ticker}")
                return results
            else:
                logger.warning(f"No details found for {ticker}")
                return None
        except Exception as e:
            logger.error(f"Failed to fetch ticker details for {ticker}: {str(e)}")
            return None
    
    def get_all_tickers(self, market: str = "stocks", 
                       active: bool = True,
                       limit: int = 1000,
                       type: Optional[str] = None,
                       search: Optional[str] = None) -> List[Dict]:
        """
        Get list of all available tickers.
        
        Args:
            market: Market type ('stocks', 'crypto', 'fx', 'otc')
            active: Only active tickers (True recommended)
            limit: Results per page
            type: Filter by security type ('CS', 'ETF', 'ADRC', etc.)
            search: Search query for ticker or company name
        
        Returns:
            List of ticker dicts with basic info
        """
        endpoint = "/v3/reference/tickers"
        params = {
            "market": market,
            "active": str(active).lower(),
            "limit": limit
        }
        
        if type:
            params['type'] = type
        if search:
            params['search'] = search
        
        try:
            logger.info(f"Fetching all {market} tickers (active={active})")
            results = self.client.get_with_pagination(endpoint, params)
            logger.info(f"Fetched {len(results)} tickers")
            return results
        except Exception as e:
            logger.error(f"Failed to fetch all tickers: {str(e)}")
            return []
    
    def get_ticker_types(self) -> List[Dict]:
        """
        Get all available ticker types.
        
        Returns:
            List of ticker type dicts with:
                - code: Type code (CS, ETF, etc.)
                - description: Human-readable description
                - asset_class: Asset class
                - locale: Locale
        """
        endpoint = "/v3/reference/tickers/types"
        
        try:
            data = self.client._get(endpoint)
            results = data.get('results', [])
            logger.info(f"Fetched {len(results)} ticker types")
            return results
        except Exception as e:
            logger.error(f"Failed to fetch ticker types: {str(e)}")
            return []
    
    def get_exchanges(self) -> List[Dict]:
        """
        Get list of stock exchanges.
        
        Returns:
            List of exchange dicts with:
                - id: Exchange ID
                - type: Exchange type
                - market: Market type
                - mic: Market Identifier Code
                - name: Exchange name
                - tape: Tape identifier
                - operating_mic: Operating MIC
        """
        endpoint = "/v3/reference/exchanges"
        
        try:
            data = self.client._get(endpoint)
            results = data.get('results', [])
            logger.info(f"Fetched {len(results)} exchanges")
            return results
        except Exception as e:
            logger.error(f"Failed to fetch exchanges: {str(e)}")
            return []
    
    def get_market_status(self) -> Dict:
        """
        Get current market status (open/closed).
        
        Returns:
            Dict with:
                - market: Market name
                - serverTime: Server timestamp
                - exchanges: Dict of exchange statuses
                - currencies: Dict of currency market statuses
        """
        endpoint = "/v1/marketstatus/now"
        
        try:
            data = self.client._get(endpoint)
            logger.info("Fetched market status")
            return data
        except Exception as e:
            logger.error(f"Failed to fetch market status: {str(e)}")
            return {}
    
    def get_market_holidays(self) -> List[Dict]:
        """
        Get list of market holidays.
        
        Returns:
            List of holiday dicts with:
                - exchange: Exchange name
                - name: Holiday name
                - status: Market status (closed, early-close)
                - date: Holiday date
                - open: Open time (if early close)
                - close: Close time (if early close)
        """
        endpoint = "/v1/marketstatus/upcoming"
        
        try:
            data = self.client._get(endpoint)
            results = data.get('holidays', [])
            logger.info(f"Fetched {len(results)} upcoming holidays")
            return results
        except Exception as e:
            logger.error(f"Failed to fetch market holidays: {str(e)}")
            return []
    
    def search_tickers(self, query: str, active: bool = True, 
                      limit: int = 10) -> List[Dict]:
        """
        Search for tickers by symbol or company name.
        
        Args:
            query: Search query (e.g., 'AAPL' or 'Apple')
            active: Only active tickers
            limit: Max results
        
        Returns:
            List of matching tickers
        """
        return self.get_all_tickers(search=query, active=active, limit=limit)


if __name__ == "__main__":
    # Test the reference client
    import os
    from dotenv import load_dotenv
    from client import PolygonClient
    
    load_dotenv()
    api_key = os.getenv("POLYGON_API_KEY")
    
    if not api_key:
        print("ERROR: POLYGON_API_KEY not found in .env file")
        exit(1)
    
    # Initialize clients
    client = PolygonClient(api_key)
    ref_client = ReferenceClient(client)
    
    # Test ticker details
    print("\n=== Testing Ticker Details for AAPL ===")
    details = ref_client.get_ticker_details("AAPL")
    if details:
        print(f"Name: {details.get('name')}")
        print(f"Market: {details.get('market')}")
        print(f"Type: {details.get('type')}")
        print(f"Primary Exchange: {details.get('primary_exchange')}")
        print(f"Market Cap: ${details.get('market_cap', 0):,.0f}")
        print(f"Employees: {details.get('total_employees', 'N/A')}")
        print(f"Website: {details.get('homepage_url', 'N/A')}")
    
    # Test ticker search
    print("\n=== Testing Ticker Search ===")
    results = ref_client.search_tickers("Tesla", limit=5)
    print(f"Found {len(results)} results for 'Tesla'")
    for ticker in results:
        print(f"  {ticker.get('ticker')}: {ticker.get('name')}")
    
    # Test market status
    print("\n=== Testing Market Status ===")
    status = ref_client.get_market_status()
    if status:
        exchanges = status.get('exchanges', {})
        for exchange, info in exchanges.items():
            print(f"{exchange}: {info}")
    
    # Test exchanges
    print("\n=== Testing Exchanges ===")
    exchanges = ref_client.get_exchanges()
    print(f"Found {len(exchanges)} exchanges")
    for exch in exchanges[:5]:
        print(f"  {exch.get('mic')}: {exch.get('name')}")
    
    client.close()
    print("\n✓ Reference client test complete!")
