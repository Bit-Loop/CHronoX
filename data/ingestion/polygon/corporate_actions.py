"""
Polygon.io Corporate Actions API Client

Handles fetching dividends, stock splits, and other corporate actions.
"""

from typing import List, Dict, Optional
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class CorporateActionsClient:
    """
    Client for fetching corporate actions (dividends, splits) from Polygon.io.
    """
    
    def __init__(self, polygon_client):
        """
        Initialize corporate actions client.
        
        Args:
            polygon_client: Instance of PolygonClient
        """
        self.client = polygon_client
    
    def get_dividends(self, ticker: Optional[str] = None, 
                     ex_dividend_date_gte: Optional[str] = None,
                     ex_dividend_date_lte: Optional[str] = None,
                     declaration_date_gte: Optional[str] = None,
                     pay_date_gte: Optional[str] = None,
                     limit: int = 1000) -> List[Dict]:
        """
        Fetch dividend history from Polygon.io.
        
        Args:
            ticker: Filter by specific ticker (e.g., 'AAPL')
            ex_dividend_date_gte: Ex-dividend date greater than or equal to (YYYY-MM-DD)
            ex_dividend_date_lte: Ex-dividend date less than or equal to (YYYY-MM-DD)
            declaration_date_gte: Declaration date greater than or equal to (YYYY-MM-DD)
            pay_date_gte: Payment date greater than or equal to (YYYY-MM-DD)
            limit: Results per page
        
        Returns:
            List of dividend dicts with keys:
                - ticker: Stock symbol
                - cash_amount: Dividend amount per share
                - currency: Currency code
                - declaration_date: Date dividend was announced
                - dividend_type: Type of dividend (CD, SC, LT, ST)
                - ex_dividend_date: Ex-dividend date
                - frequency: Payment frequency
                - pay_date: Date dividend is paid
                - record_date: Record date
        """
        endpoint = "/v3/reference/dividends"
        params = {"limit": limit}
        
        if ticker:
            params["ticker"] = ticker
        if ex_dividend_date_gte:
            params["ex_dividend_date.gte"] = ex_dividend_date_gte
        if ex_dividend_date_lte:
            params["ex_dividend_date.lte"] = ex_dividend_date_lte
        if declaration_date_gte:
            params["declaration_date.gte"] = declaration_date_gte
        if pay_date_gte:
            params["pay_date.gte"] = pay_date_gte
        
        try:
            results = self.client.get_with_pagination(endpoint, params)
            logger.info(f"Fetched {len(results)} dividend records")
            return results
        except Exception as e:
            logger.error(f"Failed to fetch dividends: {str(e)}")
            return []
    
    def get_dividends_for_ticker(self, ticker: str, 
                                since: Optional[str] = None) -> List[Dict]:
        """
        Get all dividends for a specific ticker.
        
        Args:
            ticker: Stock symbol
            since: Get dividends from this date forward (YYYY-MM-DD)
                  Default: 5 years ago
        
        Returns:
            List of dividend records for the ticker
        """
        if not since:
            since = (datetime.now() - timedelta(days=1825)).strftime('%Y-%m-%d')
        
        logger.info(f"Fetching dividends for {ticker} since {since}")
        return self.get_dividends(ticker=ticker, ex_dividend_date_gte=since)
    
    def get_splits(self, ticker: Optional[str] = None,
                  execution_date_gte: Optional[str] = None,
                  execution_date_lte: Optional[str] = None,
                  limit: int = 1000) -> List[Dict]:
        """
        Fetch stock split history from Polygon.io.
        
        Args:
            ticker: Filter by specific ticker
            execution_date_gte: Execution date greater than or equal to (YYYY-MM-DD)
            execution_date_lte: Execution date less than or equal to (YYYY-MM-DD)
            limit: Results per page
        
        Returns:
            List of split dicts with keys:
                - ticker: Stock symbol
                - execution_date: Date split was executed
                - split_from: Split ratio numerator (e.g., 1 in "1-for-4")
                - split_to: Split ratio denominator (e.g., 4 in "1-for-4")
        """
        endpoint = "/v3/reference/splits"
        params = {"limit": limit}
        
        if ticker:
            params["ticker"] = ticker
        if execution_date_gte:
            params["execution_date.gte"] = execution_date_gte
        if execution_date_lte:
            params["execution_date.lte"] = execution_date_lte
        
        try:
            results = self.client.get_with_pagination(endpoint, params)
            logger.info(f"Fetched {len(results)} split records")
            return results
        except Exception as e:
            logger.error(f"Failed to fetch splits: {str(e)}")
            return []
    
    def get_splits_for_ticker(self, ticker: str, 
                             since: Optional[str] = None) -> List[Dict]:
        """
        Get all splits for a specific ticker.
        
        Args:
            ticker: Stock symbol
            since: Get splits from this date forward (YYYY-MM-DD)
                  Default: 5 years ago
        
        Returns:
            List of split records for the ticker
        """
        if not since:
            since = (datetime.now() - timedelta(days=1825)).strftime('%Y-%m-%d')
        
        logger.info(f"Fetching splits for {ticker} since {since}")
        return self.get_splits(ticker=ticker, execution_date_gte=since)
    
    def calculate_adjustment_factor(self, splits: List[Dict], 
                                   target_date: str) -> float:
        """
        Calculate cumulative split adjustment factor for a given date.
        
        This is useful for adjusting historical prices for splits.
        
        Args:
            splits: List of split records (from get_splits)
            target_date: Date to calculate adjustment for (YYYY-MM-DD)
        
        Returns:
            Adjustment factor (multiply historical prices by this)
        """
        target = datetime.strptime(target_date, '%Y-%m-%d')
        adjustment_factor = 1.0
        
        for split in splits:
            split_date = datetime.strptime(split['execution_date'], '%Y-%m-%d')
            
            # Only apply splits that occurred after the target date
            if split_date > target:
                ratio = split['split_to'] / split['split_from']
                adjustment_factor *= ratio
        
        return adjustment_factor


if __name__ == "__main__":
    # Test the corporate actions client
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
    corp_client = CorporateActionsClient(client)
    
    # Test dividends for AAPL
    print("\n=== Testing Dividends for AAPL ===")
    dividends = corp_client.get_dividends_for_ticker("AAPL", since="2024-01-01")
    print(f"Found {len(dividends)} dividend records for AAPL in 2024")
    if dividends:
        latest = dividends[-1]
        print(f"Latest: Ex-date={latest.get('ex_dividend_date')}, Amount=${latest.get('cash_amount')}")
    
    # Test splits for TSLA
    print("\n=== Testing Splits for TSLA ===")
    splits = corp_client.get_splits_for_ticker("TSLA", since="2020-01-01")
    print(f"Found {len(splits)} split records for TSLA since 2020")
    if splits:
        for split in splits:
            ratio = f"{split.get('split_to')}-for-{split.get('split_from')}"
            print(f"Split on {split.get('execution_date')}: {ratio}")
    
    # Test adjustment factor calculation
    if splits:
        print("\n=== Testing Adjustment Factor ===")
        factor = corp_client.calculate_adjustment_factor(splits, "2020-01-01")
        print(f"Adjustment factor since 2020-01-01: {factor:.4f}")
        print(f"Example: $100 historical price → ${100 * factor:.2f} split-adjusted")
    
    client.close()
    print("\n✓ Corporate actions client test complete!")
