"""
Polygon.io News API Client

Handles fetching news articles and publishers.
"""

from typing import List, Dict, Optional
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class NewsClient:
    """
    Client for fetching news articles from Polygon.io.
    """
    
    def __init__(self, polygon_client):
        """
        Initialize news client.
        
        Args:
            polygon_client: Instance of PolygonClient
        """
        self.client = polygon_client
    
    def get_news(self, ticker: Optional[str] = None,
                published_utc_gte: Optional[str] = None,
                published_utc_lte: Optional[str] = None,
                order: str = "desc",
                limit: int = 100,
                sort: str = "published_utc") -> List[Dict]:
        """
        Fetch news articles from Polygon.io.
        
        Args:
            ticker: Filter by ticker symbol (e.g., 'AAPL')
            published_utc_gte: Published date >= (YYYY-MM-DD or ISO format)
            published_utc_lte: Published date <= (YYYY-MM-DD or ISO format)
            order: Sort order ('asc' or 'desc')
            limit: Max results per page
            sort: Sort field ('published_utc')
        
        Returns:
            List of article dicts with keys:
                - id: Article ID
                - publisher: Publisher info (name, homepage_url, logo_url, favicon_url)
                - title: Article title
                - author: Article author
                - published_utc: Publication timestamp (ISO format)
                - article_url: Full article URL
                - tickers: List of related ticker symbols
                - amp_url: AMP version URL
                - image_url: Featured image URL
                - description: Article description/excerpt
                - keywords: List of keywords
        """
        endpoint = "/v2/reference/news"
        params = {
            "order": order,
            "limit": limit,
            "sort": sort
        }
        
        if ticker:
            params['ticker'] = ticker
        if published_utc_gte:
            params['published_utc.gte'] = published_utc_gte
        if published_utc_lte:
            params['published_utc.lte'] = published_utc_lte
        
        try:
            results = self.client.get_with_pagination(endpoint, params)
            logger.info(f"Fetched {len(results)} news articles")
            return results
        except Exception as e:
            logger.error(f"Failed to fetch news: {str(e)}")
            return []
    
    def get_news_for_ticker(self, ticker: str, 
                           days_back: int = 30,
                           limit: int = 100) -> List[Dict]:
        """
        Get recent news for a specific ticker.
        
        Args:
            ticker: Stock symbol
            days_back: How many days back to fetch news
            limit: Max articles to return
        
        Returns:
            List of news articles
        """
        since = (datetime.utcnow() - timedelta(days=days_back)).strftime('%Y-%m-%d')
        
        logger.info(f"Fetching news for {ticker} from last {days_back} days")
        return self.get_news(ticker=ticker, published_utc_gte=since, limit=limit)
    
    def get_latest_news(self, limit: int = 50) -> List[Dict]:
        """
        Get latest market news across all tickers.
        
        Args:
            limit: Max articles to return
        
        Returns:
            List of latest news articles
        """
        logger.info(f"Fetching latest {limit} news articles")
        return self.get_news(limit=limit, order='desc')
    
    def get_news_by_date_range(self, start_date: str, end_date: str,
                               ticker: Optional[str] = None) -> List[Dict]:
        """
        Get news for a specific date range.
        
        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            ticker: Optional ticker filter
        
        Returns:
            List of news articles in date range
        """
        logger.info(f"Fetching news from {start_date} to {end_date}")
        return self.get_news(
            ticker=ticker,
            published_utc_gte=start_date,
            published_utc_lte=end_date,
            order='asc'
        )
    
    def extract_sentiment_keywords(self, article: Dict) -> Dict:
        """
        Extract potential sentiment-relevant information from article.
        
        Args:
            article: News article dict
        
        Returns:
            Dict with extracted sentiment features:
                - keywords: Article keywords
                - has_negative_words: Boolean indicating negative sentiment indicators
                - has_positive_words: Boolean indicating positive sentiment indicators
                - tickers_mentioned: Number of tickers mentioned
        """
        # Common negative sentiment words
        negative_words = [
            'plunge', 'crash', 'drop', 'fall', 'decline', 'loss', 'losses',
            'down', 'sink', 'slump', 'tumble', 'worse', 'worst', 'weak',
            'concern', 'worry', 'fear', 'risk', 'threat', 'problem'
        ]
        
        # Common positive sentiment words
        positive_words = [
            'surge', 'rally', 'rise', 'gain', 'gains', 'up', 'jump',
            'soar', 'climb', 'advance', 'better', 'best', 'strong',
            'growth', 'profit', 'profits', 'beat', 'exceed', 'outperform'
        ]
        
        title = article.get('title', '').lower()
        description = article.get('description', '').lower()
        text = f"{title} {description}"
        
        has_negative = any(word in text for word in negative_words)
        has_positive = any(word in text for word in positive_words)
        
        return {
            'keywords': article.get('keywords', []),
            'has_negative_words': has_negative,
            'has_positive_words': has_positive,
            'tickers_mentioned': len(article.get('tickers', [])),
            'title': article.get('title', ''),
            'published_utc': article.get('published_utc', '')
        }


if __name__ == "__main__":
    # Test the news client
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
    news_client = NewsClient(client)
    
    # Test latest news
    print("\n=== Testing Latest News ===")
    latest = news_client.get_latest_news(limit=5)
    print(f"Fetched {len(latest)} latest articles")
    if latest:
        article = latest[0]
        print(f"\nLatest Article:")
        print(f"  Title: {article.get('title')}")
        print(f"  Publisher: {article.get('publisher', {}).get('name')}")
        print(f"  Published: {article.get('published_utc')}")
        print(f"  Tickers: {', '.join(article.get('tickers', []))}")
        print(f"  URL: {article.get('article_url')}")
    
    # Test news for specific ticker
    print("\n=== Testing News for AAPL ===")
    aapl_news = news_client.get_news_for_ticker("AAPL", days_back=7, limit=10)
    print(f"Fetched {len(aapl_news)} articles for AAPL")
    
    # Test sentiment extraction
    if aapl_news:
        print("\n=== Testing Sentiment Extraction ===")
        for article in aapl_news[:3]:
            sentiment = news_client.extract_sentiment_keywords(article)
            print(f"\nTitle: {sentiment['title'][:80]}...")
            print(f"  Positive: {sentiment['has_positive_words']}")
            print(f"  Negative: {sentiment['has_negative_words']}")
            print(f"  Keywords: {', '.join(sentiment['keywords'][:5])}")
    
    client.close()
    print("\n✓ News client test complete!")
