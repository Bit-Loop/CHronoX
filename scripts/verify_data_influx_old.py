#!/usr/bin/env python3
"""
Data Verification Tool for ChronoX Trading Bot

Verifies data quality and completeness in TimescaleDB after backfill.
"""

import os
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List
import logging

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from data.storage.timescale_writer import TimescaleWriter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class DataVerifier:
    """
    Verifies data quality and completeness in TimescaleDB.
    """
    
    def __init__(self, db_writer: TimescaleWriter):
        """
        Initialize data verifier.
        
        Args:
            db_writer: TimescaleDB writer instance
        """
        self.db = db_writer
        logger.info("Data verifier initialized")
    
    def check_ticker_data(self, ticker: str, bucket: str) -> Dict:
        """
        Check data completeness for a specific ticker.
        
        Args:
            ticker: Stock symbol
            bucket: InfluxDB bucket name
        
        Returns:
            Dict with statistics
        """
        query = f'''
        from(bucket: "{bucket}")
          |> range(start: -5y)
          |> filter(fn: (r) => r["ticker"] == "{ticker}")
          |> filter(fn: (r) => r["_measurement"] == "market_data")
          |> count()
          |> yield(name: "count")
        '''
        
        try:
            result = self.query_api.query(query, org=self.org)
            
            if result and len(result) > 0:
                count = 0
                for table in result:
                    for record in table.records:
                        count += record.get_value()
                
                return {
                    'ticker': ticker,
                    'bucket': bucket,
                    'total_points': count,
                    'status': 'OK' if count > 0 else 'NO DATA'
                }
            else:
                return {
                    'ticker': ticker,
                    'bucket': bucket,
                    'total_points': 0,
                    'status': 'NO DATA'
                }
        
        except Exception as e:
            logger.error(f"Error checking {ticker}: {str(e)}")
            return {
                'ticker': ticker,
                'bucket': bucket,
                'error': str(e),
                'status': 'ERROR'
            }
    
    def check_date_gaps(self, ticker: str, bucket: str, timeframe: str = "1day") -> List[Dict]:
        """
        Check for gaps in time series data.
        
        Args:
            ticker: Stock symbol
            bucket: InfluxDB bucket
            timeframe: Data timeframe
        
        Returns:
            List of gap information
        """
        query = f'''
        from(bucket: "{bucket}")
          |> range(start: -1y)
          |> filter(fn: (r) => r["ticker"] == "{ticker}")
          |> filter(fn: (r) => r["timeframe"] == "{timeframe}")
          |> filter(fn: (r) => r["_measurement"] == "market_data")
          |> filter(fn: (r) => r["_field"] == "close")
          |> aggregateWindow(every: 1d, fn: count)
          |> filter(fn: (r) => r["_value"] == 0)
          |> yield(name: "gaps")
        '''
        
        try:
            result = self.query_api.query(query, org=self.org)
            gaps = []
            
            for table in result:
                for record in table.records:
                    gaps.append({
                        'date': record.get_time(),
                        'ticker': ticker,
                        'timeframe': timeframe
                    })
            
            if gaps:
                logger.warning(f"Found {len(gaps)} gaps for {ticker} {timeframe}")
            else:
                logger.info(f"No gaps found for {ticker} {timeframe}")
            
            return gaps
        
        except Exception as e:
            logger.error(f"Error checking gaps for {ticker}: {str(e)}")
            return []
    
    def get_data_summary(self, bucket: str) -> Dict:
        """
        Get overall data summary for a bucket.
        
        Args:
            bucket: InfluxDB bucket
        
        Returns:
            Summary statistics
        """
        # Count unique tickers
        query_tickers = f'''
        import "influxdata/influxdb/schema"
        
        schema.tagValues(
          bucket: "{bucket}",
          tag: "ticker",
          predicate: (r) => r._measurement == "market_data",
          start: -5y
        )
        '''
        
        # Count total points
        query_points = f'''
        from(bucket: "{bucket}")
          |> range(start: -5y)
          |> filter(fn: (r) => r["_measurement"] == "market_data")
          |> count()
          |> group()
          |> sum()
        '''
        
        try:
            # Get tickers
            ticker_result = self.query_api.query(query_tickers, org=self.org)
            tickers = []
            for table in ticker_result:
                for record in table.records:
                    tickers.append(record.get_value())
            
            # Get point count
            points_result = self.query_api.query(query_points, org=self.org)
            total_points = 0
            for table in points_result:
                for record in table.records:
                    total_points += record.get_value()
            
            return {
                'bucket': bucket,
                'unique_tickers': len(tickers),
                'total_points': total_points,
                'tickers': tickers[:50]  # First 50 tickers
            }
        
        except Exception as e:
            logger.error(f"Error getting summary for {bucket}: {str(e)}")
            return {
                'bucket': bucket,
                'error': str(e)
            }
    
    def verify_ohlcv_integrity(self, ticker: str, bucket: str, sample_size: int = 100) -> Dict:
        """
        Verify OHLCV data integrity (check for invalid values).
        
        Args:
            ticker: Stock symbol
            bucket: InfluxDB bucket
            sample_size: Number of samples to check
        
        Returns:
            Integrity report
        """
        query = f'''
        from(bucket: "{bucket}")
          |> range(start: -30d)
          |> filter(fn: (r) => r["ticker"] == "{ticker}")
          |> filter(fn: (r) => r["_measurement"] == "market_data")
          |> limit(n: {sample_size})
          |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        
        try:
            result = self.query_api.query(query, org=self.org)
            
            issues = []
            valid_count = 0
            
            for table in result:
                for record in table.records:
                    values = record.values
                    
                    # Extract OHLCV
                    o = values.get('open', 0)
                    h = values.get('high', 0)
                    l = values.get('low', 0)
                    c = values.get('close', 0)
                    v = values.get('volume', 0)
                    
                    # Check for invalid values
                    if h < l:
                        issues.append(f"High < Low at {values.get('_time')}")
                    if h < o or h < c:
                        issues.append(f"High < Open/Close at {values.get('_time')}")
                    if l > o or l > c:
                        issues.append(f"Low > Open/Close at {values.get('_time')}")
                    if any(x <= 0 for x in [o, h, l, c]):
                        issues.append(f"Zero/negative price at {values.get('_time')}")
                    if v < 0:
                        issues.append(f"Negative volume at {values.get('_time')}")
                    
                    if not issues:
                        valid_count += 1
            
            return {
                'ticker': ticker,
                'samples_checked': sample_size,
                'valid_samples': valid_count,
                'issues_found': len(issues),
                'issues': issues[:10]  # First 10 issues
            }
        
        except Exception as e:
            logger.error(f"Error verifying integrity for {ticker}: {str(e)}")
            return {
                'ticker': ticker,
                'error': str(e)
            }
    
    def close(self):
        """Close InfluxDB client"""
        self.client.close()


def main():
    """Main entry point"""
    load_dotenv()
    
    # Configure InfluxDB
    url = os.getenv('INFLUX_URL', 'http://localhost:8086')
    token = os.getenv('INFLUX_TOKEN', '')
    org = os.getenv('INFLUX_ORG', 'chronox')
    bucket = os.getenv('INFLUX_BUCKET_MARKET', 'market_data_5y')
    
    if not token:
        logger.error("INFLUX_TOKEN not found in environment")
        sys.exit(1)
    
    verifier = DataVerifier(url, token, org)
    
    try:
        print("\n" + "="*60)
        print("DATA VERIFICATION REPORT")
        print("="*60 + "\n")
        
        # Overall summary
        print("=== Bucket Summary ===")
        summary = verifier.get_data_summary(bucket)
        print(f"Bucket: {summary.get('bucket')}")
        print(f"Unique Tickers: {summary.get('unique_tickers', 0)}")
        print(f"Total Data Points: {summary.get('total_points', 0):,}")
        
        if 'tickers' in summary:
            print(f"\nSample Tickers: {', '.join(summary['tickers'][:10])}")
        
        # Check specific tickers if available
        if 'tickers' in summary and summary['tickers']:
            print("\n=== Ticker Data Checks ===")
            test_tickers = summary['tickers'][:5]  # Check first 5 tickers
            
            for ticker in test_tickers:
                print(f"\n{ticker}:")
                
                # Check data completeness
                stats = verifier.check_ticker_data(ticker, bucket)
                print(f"  Total Points: {stats.get('total_points', 0):,}")
                print(f"  Status: {stats.get('status')}")
                
                # Check data integrity
                integrity = verifier.verify_ohlcv_integrity(ticker, bucket)
                print(f"  Valid Samples: {integrity.get('valid_samples', 0)}/{integrity.get('samples_checked', 0)}")
                if integrity.get('issues_found', 0) > 0:
                    print(f"  Issues: {integrity.get('issues_found')} found")
                    for issue in integrity.get('issues', [])[:3]:
                        print(f"    - {issue}")
        
        print("\n" + "="*60)
        print("VERIFICATION COMPLETE")
        print("="*60 + "\n")
    
    finally:
        verifier.close()


if __name__ == "__main__":
    main()
