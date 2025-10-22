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
    
    def check_ticker_data(self, ticker: str, timeframe: str = '1day') -> Dict:
        """
        Check data completeness for a specific ticker.
        
        Args:
            ticker: Stock symbol
            timeframe: Timeframe to check ('1day', '1min', etc.)
            
        Returns:
            Dict with statistics
        """
        logger.info(f"Checking data for {ticker} ({timeframe})")
        
        try:
            # Query to get count, date range, and gaps
            query = """
                SELECT 
                    COUNT(*) as count,
                    MIN(timestamp) as start_date,
                    MAX(timestamp) as end_date
                FROM market_data
                WHERE symbol = %s
                    AND timeframe = %s
            """
            
            result = self.db.execute_query(query, (ticker, timeframe))
            
            if not result:
                logger.warning(f"No data found for {ticker}")
                return {
                    'ticker': ticker,
                    'timeframe': timeframe,
                    'count': 0,
                    'start_date': None,
                    'end_date': None,
                    'status': 'NO_DATA'
                }
            
            row = result[0]
            count = row[0]
            start_date = row[1]
            end_date = row[2]
            
            # Calculate expected trading days (rough estimate)
            if start_date and end_date:
                days_diff = (end_date - start_date).days
                # Assume ~252 trading days per year
                expected_points = int(days_diff * (252 / 365))
                completeness = (count / expected_points * 100) if expected_points > 0 else 0
            else:
                expected_points = 0
                completeness = 0
            
            stats = {
                'ticker': ticker,
                'timeframe': timeframe,
                'count': count,
                'start_date': start_date,
                'end_date': end_date,
                'expected_points': expected_points,
                'completeness': f"{completeness:.1f}%",
                'status': 'OK' if count > 0 else 'NO_DATA'
            }
            
            logger.info(f"  ✓ {ticker}: {count} records from {start_date} to {end_date} ({completeness:.1f}% complete)")
            return stats
            
        except Exception as e:
            logger.error(f"Error checking {ticker}: {e}")
            return {
                'ticker': ticker,
                'timeframe': timeframe,
                'error': str(e),
                'status': 'ERROR'
            }
    
    def check_all_tickers(self, timeframe: str = '1day') -> List[Dict]:
        """
        Check data for all tickers in database.
        
        Args:
            timeframe: Timeframe to check
            
        Returns:
            List of stats for each ticker
        """
        logger.info(f"Checking all tickers ({timeframe})")
        
        try:
            # Get list of unique tickers
            query = """
                SELECT DISTINCT symbol
                FROM market_data
                WHERE timeframe = %s
                ORDER BY symbol
            """
            
            results = self.db.execute_query(query, (timeframe,))
            tickers = [row[0] for row in results]
            
            logger.info(f"Found {len(tickers)} tickers")
            
            # Check each ticker
            all_stats = []
            for ticker in tickers:
                stats = self.check_ticker_data(ticker, timeframe)
                all_stats.append(stats)
            
            return all_stats
            
        except Exception as e:
            logger.error(f"Error checking all tickers: {e}")
            return []
    
    def check_data_gaps(self, ticker: str, timeframe: str = '1day') -> List[Dict]:
        """
        Find gaps in time-series data.
        
        Args:
            ticker: Stock symbol
            timeframe: Timeframe to check
            
        Returns:
            List of gaps found
        """
        logger.info(f"Checking gaps for {ticker} ({timeframe})")
        
        try:
            # Query to find gaps (missing trading days)
            query = """
                WITH date_series AS (
                    SELECT timestamp,
                           LEAD(timestamp) OVER (ORDER BY timestamp) as next_timestamp
                    FROM market_data
                    WHERE symbol = %s
                        AND timeframe = %s
                    ORDER BY timestamp
                )
                SELECT 
                    timestamp as gap_start,
                    next_timestamp as gap_end,
                    EXTRACT(DAY FROM (next_timestamp - timestamp)) as gap_days
                FROM date_series
                WHERE next_timestamp IS NOT NULL
                    AND (next_timestamp - timestamp) > INTERVAL '3 days'
                ORDER BY timestamp
                LIMIT 100
            """
            
            results = self.db.execute_query(query, (ticker, timeframe))
            
            gaps = []
            for row in results:
                gap = {
                    'gap_start': row[0],
                    'gap_end': row[1],
                    'gap_days': row[2]
                }
                gaps.append(gap)
                logger.warning(f"  Gap found: {row[0]} to {row[1]} ({row[2]:.0f} days)")
            
            if not gaps:
                logger.info(f"  ✓ No significant gaps found for {ticker}")
            else:
                logger.warning(f"  ⚠ Found {len(gaps)} gaps for {ticker}")
            
            return gaps
            
        except Exception as e:
            logger.error(f"Error checking gaps for {ticker}: {e}")
            return []
    
    def get_summary_stats(self) -> Dict:
        """
        Get overall database statistics.
        
        Returns:
            Dict with summary stats
        """
        logger.info("Getting summary statistics")
        
        try:
            # Overall stats
            query = """
                SELECT 
                    timeframe,
                    COUNT(DISTINCT symbol) as ticker_count,
                    COUNT(*) as total_records,
                    MIN(timestamp) as earliest_date,
                    MAX(timestamp) as latest_date
                FROM market_data
                GROUP BY timeframe
                ORDER BY timeframe
            """
            
            results = self.db.execute_query(query)
            
            stats = []
            total_records = 0
            unique_tickers = set()
            
            for row in results:
                timeframe_stats = {
                    'timeframe': row[0],
                    'ticker_count': row[1],
                    'total_records': row[2],
                    'earliest_date': row[3],
                    'latest_date': row[4]
                }
                stats.append(timeframe_stats)
                total_records += row[2]
                
                # Get unique tickers for this timeframe
                ticker_query = "SELECT DISTINCT symbol FROM market_data WHERE timeframe = %s"
                ticker_results = self.db.execute_query(ticker_query, (row[0],))
                unique_tickers.update([t[0] for t in ticker_results])
                
                logger.info(f"  {row[0]}: {row[1]} tickers, {row[2]:,} records ({row[3]} to {row[4]})")
            
            summary = {
                'total_records': total_records,
                'unique_tickers': len(unique_tickers),
                'timeframe_stats': stats
            }
            
            logger.info(f"\nSummary: {total_records:,} total records across {len(unique_tickers)} unique tickers")
            return summary
            
        except Exception as e:
            logger.error(f"Error getting summary stats: {e}")
            return {}
    
    def close(self):
        """Close database connection"""
        self.db.close()
        logger.info("Data verifier closed")


def main():
    """Main verification workflow"""
    load_dotenv()
    
    logger.info("=" * 80)
    logger.info("ChronoX Data Verification Tool (TimescaleDB)")
    logger.info("=" * 80)
    
    # Initialize database connection
    try:
        db = TimescaleWriter()
        if not db.test_connection():
            logger.error("Failed to connect to TimescaleDB")
            return 1
        logger.info("✓ Connected to TimescaleDB")
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        return 1
    
    # Initialize verifier
    verifier = DataVerifier(db)
    
    try:
        # Get summary statistics
        logger.info("\n" + "=" * 80)
        logger.info("SUMMARY STATISTICS")
        logger.info("=" * 80)
        summary = verifier.get_summary_stats()
        
        # Check specific tickers (if provided via command line)
        if len(sys.argv) > 1:
            tickers = sys.argv[1].split(',')
            logger.info("\n" + "=" * 80)
            logger.info(f"CHECKING SPECIFIC TICKERS: {', '.join(tickers)}")
            logger.info("=" * 80)
            
            for ticker in tickers:
                ticker = ticker.strip().upper()
                
                # Check daily data
                stats = verifier.check_ticker_data(ticker, '1day')
                
                # Check for gaps
                if stats.get('count', 0) > 0:
                    gaps = verifier.check_data_gaps(ticker, '1day')
        else:
            # Check all tickers
            logger.info("\n" + "=" * 80)
            logger.info("CHECKING ALL TICKERS (Daily Data)")
            logger.info("=" * 80)
            all_stats = verifier.check_all_tickers('1day')
            
            # Show tickers with issues
            issues = [s for s in all_stats if s.get('status') != 'OK']
            if issues:
                logger.warning(f"\n⚠ Found {len(issues)} tickers with issues:")
                for stat in issues:
                    logger.warning(f"  - {stat['ticker']}: {stat.get('status', 'UNKNOWN')}")
        
        logger.info("\n" + "=" * 80)
        logger.info("VERIFICATION COMPLETE")
        logger.info("=" * 80)
        
    except KeyboardInterrupt:
        logger.info("\nInterrupted by user")
    except Exception as e:
        logger.error(f"Verification error: {e}", exc_info=True)
        return 1
    finally:
        verifier.close()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
