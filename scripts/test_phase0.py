#!/usr/bin/env python3
"""
Quick Test Script for Phase 0 Components

Tests all Polygon.io API clients and InfluxDB connectivity.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from data.ingestion.polygon.client import PolygonClient
from data.ingestion.polygon.aggregates import AggregatesClient, SnapshotClient
from data.ingestion.polygon.corporate_actions import CorporateActionsClient
from data.ingestion.polygon.reference import ReferenceClient
from data.ingestion.polygon.news import NewsClient
from data.ingestion.polygon.indicators import IndicatorsClient
from data.storage.influx_writer import InfluxWriter, InfluxConfig

def test_polygon_connection(api_key: str) -> bool:
    """Test Polygon.io API connection"""
    print("\n=== Testing Polygon.io Connection ===")
    try:
        client = PolygonClient(api_key)
        if client.test_connection():
            print("✓ Polygon.io API connection successful")
            client.close()
            return True
        else:
            print("✗ Polygon.io API connection failed")
            return False
    except Exception as e:
        print(f"✗ Error: {str(e)}")
        return False

def test_aggregates_client(api_key: str) -> bool:
    """Test aggregates client"""
    print("\n=== Testing Aggregates Client ===")
    try:
        client = PolygonClient(api_key)
        agg_client = AggregatesClient(client)
        
        # Test daily bars
        bars = agg_client.get_daily_bars("AAPL", "2024-10-01", "2024-10-10")
        if bars:
            print(f"✓ Fetched {len(bars)} daily bars for AAPL")
            print(f"  Sample: O={bars[0]['o']}, C={bars[0]['c']}, V={bars[0]['v']}")
            client.close()
            return True
        else:
            print("✗ No bars fetched")
            return False
    except Exception as e:
        print(f"✗ Error: {str(e)}")
        return False

def test_reference_client(api_key: str) -> bool:
    """Test reference client"""
    print("\n=== Testing Reference Client ===")
    try:
        client = PolygonClient(api_key)
        ref_client = ReferenceClient(client)
        
        details = ref_client.get_ticker_details("AAPL")
        if details:
            print(f"✓ Fetched ticker details for AAPL")
            print(f"  Name: {details.get('name')}")
            print(f"  Exchange: {details.get('primary_exchange')}")
            client.close()
            return True
        else:
            print("✗ No details fetched")
            return False
    except Exception as e:
        print(f"✗ Error: {str(e)}")
        return False

def test_news_client(api_key: str) -> bool:
    """Test news client"""
    print("\n=== Testing News Client ===")
    try:
        client = PolygonClient(api_key)
        news_client = NewsClient(client)
        
        articles = news_client.get_latest_news(limit=5)
        if articles:
            print(f"✓ Fetched {len(articles)} news articles")
            print(f"  Latest: {articles[0].get('title', 'N/A')[:60]}...")
            client.close()
            return True
        else:
            print("✗ No articles fetched")
            return False
    except Exception as e:
        print(f"✗ Error: {str(e)}")
        return False

def test_influxdb_connection(config: InfluxConfig) -> bool:
    """Test InfluxDB connection"""
    print("\n=== Testing InfluxDB Connection ===")
    try:
        writer = InfluxWriter(config, async_write=False)
        
        # Try to write test data
        test_bars = [{
            't': 1696176000000,
            'o': 100.0,
            'h': 101.0,
            'l': 99.0,
            'c': 100.5,
            'v': 1000
        }]
        
        count = writer.write_ohlcv_bars("TEST", test_bars, timeframe="1min")
        if count > 0:
            print(f"✓ InfluxDB connection successful")
            print(f"  Wrote {count} test points")
            writer.close()
            return True
        else:
            print("✗ Failed to write to InfluxDB")
            return False
    except Exception as e:
        print(f"✗ Error: {str(e)}")
        return False

def main():
    """Main entry point"""
    print("\n" + "="*60)
    print("CHRONOX PHASE 0 COMPONENT TEST")
    print("="*60)
    
    # Load environment
    load_dotenv()
    
    polygon_api_key = os.getenv("POLYGON_API_KEY")
    influx_url = os.getenv("INFLUX_URL", "http://localhost:8086")
    influx_token = os.getenv("INFLUX_TOKEN", "")
    influx_org = os.getenv("INFLUX_ORG", "chronox")
    influx_bucket = os.getenv("INFLUX_BUCKET_MARKET", "market_data_5y")
    
    # Check environment
    if not polygon_api_key:
        print("\n✗ POLYGON_API_KEY not found in .env file")
        print("  Please add your Polygon.io API key to .env")
        sys.exit(1)
    
    if not influx_token:
        print("\n✗ INFLUX_TOKEN not found in .env file")
        print("  Please configure InfluxDB and add token to .env")
        sys.exit(1)
    
    # Run tests
    results = []
    
    results.append(("Polygon.io Connection", test_polygon_connection(polygon_api_key)))
    results.append(("Aggregates Client", test_aggregates_client(polygon_api_key)))
    results.append(("Reference Client", test_reference_client(polygon_api_key)))
    results.append(("News Client", test_news_client(polygon_api_key)))
    
    # Configure InfluxDB
    influx_config = InfluxConfig(
        url=influx_url,
        token=influx_token,
        org=influx_org,
        bucket_market=influx_bucket
    )
    
    results.append(("InfluxDB Connection", test_influxdb_connection(influx_config)))
    
    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    
    for test_name, success in results:
        status = "✓ PASS" if success else "✗ FAIL"
        print(f"{test_name:.<40} {status}")
    
    total = len(results)
    passed = sum(1 for _, success in results if success)
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n✓ All tests passed! Phase 0 is ready to go.")
        sys.exit(0)
    else:
        print(f"\n✗ {total - passed} test(s) failed. Please fix issues before proceeding.")
        sys.exit(1)

if __name__ == "__main__":
    main()
