"""
Flat File Parser for Polygon.io Data

Parses and extracts data from compressed Polygon flat files.
"""

import gzip
import json
import csv
from pathlib import Path
from typing import Iterator, Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


class FlatFileParser:
    """
    Parser for Polygon.io flat files in various formats.
    
    Supports:
        - Compressed files (.gz)
        - JSON (line-delimited)
        - CSV
    """
    
    @staticmethod
    def parse_file(filepath: Path, file_format: Optional[str] = None) -> Iterator[Dict]:
        """
        Parse a flat file and yield records.
        
        Args:
            filepath: Path to file
            file_format: File format ('json', 'csv', or None for auto-detect)
        
        Yields:
            Record dictionaries
        """
        if not filepath.exists():
            logger.error(f"File not found: {filepath}")
            return
        
        # Auto-detect format if not specified
        if not file_format:
            if '.json' in filepath.name:
                file_format = 'json'
            elif '.csv' in filepath.name:
                file_format = 'csv'
            else:
                logger.error(f"Cannot auto-detect format for {filepath.name}")
                return
        
        # Determine if file is compressed
        is_compressed = filepath.suffix == '.gz'
        
        logger.info(f"Parsing {filepath.name} (format={file_format}, compressed={is_compressed})")
        
        try:
            if is_compressed:
                yield from FlatFileParser._parse_compressed(filepath, file_format)
            else:
                yield from FlatFileParser._parse_uncompressed(filepath, file_format)
        except Exception as e:
            logger.error(f"Error parsing {filepath.name}: {str(e)}")
    
    @staticmethod
    def _parse_compressed(filepath: Path, file_format: str) -> Iterator[Dict]:
        """Parse compressed file"""
        with gzip.open(filepath, 'rt', encoding='utf-8') as f:
            if file_format == 'json':
                # Line-delimited JSON
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError as e:
                        logger.error(f"JSON parse error at line {line_num}: {e}")
            
            elif file_format == 'csv':
                reader = csv.DictReader(f)
                for row in reader:
                    yield row
    
    @staticmethod
    def _parse_uncompressed(filepath: Path, file_format: str) -> Iterator[Dict]:
        """Parse uncompressed file"""
        with open(filepath, 'r', encoding='utf-8') as f:
            if file_format == 'json':
                # Line-delimited JSON
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError as e:
                        logger.error(f"JSON parse error at line {line_num}: {e}")
            
            elif file_format == 'csv':
                reader = csv.DictReader(f)
                for row in reader:
                    yield row
    
    @staticmethod
    def batch_records(records: Iterator[Dict], batch_size: int = 5000) -> Iterator[List[Dict]]:
        """
        Batch records for efficient database insertion.
        
        Args:
            records: Iterator of record dicts
            batch_size: Number of records per batch
        
        Yields:
            Batches of records
        """
        batch = []
        for record in records:
            batch.append(record)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        
        if batch:
            yield batch
    
    @staticmethod
    def parse_trades_file(filepath: Path) -> Iterator[Dict]:
        """
        Parse trades flat file.
        
        Expected fields:
            - t: timestamp (nanoseconds)
            - y: ticker
            - p: price
            - s: size
            - x: exchange
            - c: conditions
        """
        return FlatFileParser.parse_file(filepath)
    
    @staticmethod
    def parse_quotes_file(filepath: Path) -> Iterator[Dict]:
        """
        Parse quotes flat file.
        
        Expected fields:
            - t: timestamp (nanoseconds)
            - y: ticker
            - bp: bid price
            - bs: bid size
            - ap: ask price
            - as: ask size
            - x: exchange
            - c: conditions
        """
        return FlatFileParser.parse_file(filepath)
    
    @staticmethod
    def parse_aggregates_file(filepath: Path) -> Iterator[Dict]:
        """
        Parse aggregates (OHLCV) flat file.
        
        Expected fields:
            - t: timestamp
            - o: open
            - h: high
            - l: low
            - c: close
            - v: volume
            - vw: volume-weighted average price
            - n: number of transactions
        """
        return FlatFileParser.parse_file(filepath)
    
    @staticmethod
    def count_records(filepath: Path) -> int:
        """
        Count total records in a file without loading into memory.
        
        Args:
            filepath: Path to file
        
        Returns:
            Total record count
        """
        count = 0
        
        try:
            for _ in FlatFileParser.parse_file(filepath):
                count += 1
            
            logger.info(f"Counted {count:,} records in {filepath.name}")
            return count
        
        except Exception as e:
            logger.error(f"Error counting records: {str(e)}")
            return 0
    
    @staticmethod
    def sample_records(filepath: Path, n: int = 10) -> List[Dict]:
        """
        Get sample records from file.
        
        Args:
            filepath: Path to file
            n: Number of samples
        
        Returns:
            List of sample records
        """
        samples = []
        
        try:
            for i, record in enumerate(FlatFileParser.parse_file(filepath)):
                if i >= n:
                    break
                samples.append(record)
            
            logger.info(f"Sampled {len(samples)} records from {filepath.name}")
            return samples
        
        except Exception as e:
            logger.error(f"Error sampling records: {str(e)}")
            return []
    
    @staticmethod
    def filter_by_ticker(records: Iterator[Dict], tickers: List[str]) -> Iterator[Dict]:
        """
        Filter records by ticker symbols.
        
        Args:
            records: Iterator of records
            tickers: List of ticker symbols to keep
        
        Yields:
            Filtered records
        """
        ticker_set = set(t.upper() for t in tickers)
        
        for record in records:
            # Try common ticker field names
            ticker = record.get('ticker') or record.get('sym') or record.get('y') or record.get('T')
            
            if ticker and ticker.upper() in ticker_set:
                yield record
    
    @staticmethod
    def convert_timestamps(records: Iterator[Dict], 
                          timestamp_fields: List[str] = ['t', 'timestamp']) -> Iterator[Dict]:
        """
        Convert nanosecond timestamps to milliseconds.
        
        Polygon.io often uses nanosecond timestamps, but InfluxDB uses milliseconds.
        
        Args:
            records: Iterator of records
            timestamp_fields: Fields to convert
        
        Yields:
            Records with converted timestamps
        """
        for record in records:
            for field in timestamp_fields:
                if field in record:
                    try:
                        # Convert nanoseconds to milliseconds
                        ns_timestamp = int(record[field])
                        record[field] = ns_timestamp // 1_000_000
                    except (ValueError, TypeError):
                        logger.warning(f"Could not convert timestamp: {record[field]}")
            
            yield record


if __name__ == "__main__":
    # Test the parser
    import sys
    from pathlib import Path
    
    logging.basicConfig(level=logging.INFO)
    
    print("\n=== Flat File Parser Test ===")
    
    # Check if test file exists
    test_dir = Path("./data/flat_files")
    
    if not test_dir.exists():
        print(f"Test directory not found: {test_dir}")
        print("Create some test files first using the downloader")
        sys.exit(1)
    
    # Find test files
    test_files = list(test_dir.glob("*.gz")) + list(test_dir.glob("*.json")) + list(test_dir.glob("*.csv"))
    
    if not test_files:
        print("No test files found in ./data/flat_files")
        print("Download some flat files first using flat_files.py")
        sys.exit(1)
    
    # Test with first file
    test_file = test_files[0]
    print(f"\nTest file: {test_file.name}")
    
    # Count records
    print("\n=== Counting Records ===")
    total = FlatFileParser.count_records(test_file)
    print(f"Total records: {total:,}")
    
    # Sample records
    print("\n=== Sampling Records ===")
    samples = FlatFileParser.sample_records(test_file, n=5)
    for i, sample in enumerate(samples, 1):
        print(f"\nSample {i}:")
        for key, value in list(sample.items())[:10]:  # Show first 10 fields
            print(f"  {key}: {value}")
    
    # Test batching
    print("\n=== Testing Batch Processing ===")
    records = FlatFileParser.parse_file(test_file)
    batches = FlatFileParser.batch_records(records, batch_size=1000)
    
    batch_count = 0
    record_count = 0
    
    for batch in batches:
        batch_count += 1
        record_count += len(batch)
        if batch_count <= 3:
            print(f"Batch {batch_count}: {len(batch)} records")
    
    print(f"Total: {batch_count} batches, {record_count:,} records")
    
    print("\n✓ Flat file parser test complete!")
