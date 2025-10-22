"""
Polygon.io Flat File Downloader

Efficiently downloads bulk historical data files from Polygon.io.
"""

import aiohttp
import asyncio
from pathlib import Path
from typing import List, Optional, Dict
import hashlib
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class FlatFileDownloader:
    """
    Asynchronous downloader for Polygon.io flat files.
    
    Flat files provide bulk historical data in compressed format,
    which is more efficient than making thousands of API calls.
    """
    
    def __init__(self, api_key: str, download_dir: str = "./data/flat_files"):
        """
        Initialize flat file downloader.
        
        Args:
            api_key: Polygon.io API key
            download_dir: Directory to save downloaded files
        """
        self.api_key = api_key
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.concurrent_downloads = 6  # Parallel downloads
        
        logger.info(f"Flat file downloader initialized. Download directory: {self.download_dir}")
    
    async def download_file(self, session: aiohttp.ClientSession, 
                           url: str, output_path: Path, 
                           max_retries: int = 3) -> bool:
        """
        Download a single file with retry logic.
        
        Args:
            session: aiohttp session
            url: File URL
            output_path: Local file path to save
            max_retries: Maximum retry attempts
        
        Returns:
            True if successful, False otherwise
        """
        for attempt in range(max_retries):
            try:
                logger.info(f"Downloading {output_path.name} (attempt {attempt + 1}/{max_retries})")
                
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=600)) as response:
                    if response.status == 200:
                        # Stream to disk to avoid RAM spikes with large files
                        total_size = int(response.headers.get('content-length', 0))
                        downloaded = 0
                        
                        with open(output_path, 'wb') as f:
                            async for chunk in response.content.iter_chunked(8192):
                                f.write(chunk)
                                downloaded += len(chunk)
                        
                        size_mb = downloaded / (1024 * 1024)
                        logger.info(f"✓ Downloaded {output_path.name} ({size_mb:.2f} MB)")
                        return True
                    
                    elif response.status == 429:
                        # Rate limited
                        retry_after = int(response.headers.get('Retry-After', 60))
                        logger.warning(f"Rate limited. Waiting {retry_after} seconds...")
                        await asyncio.sleep(retry_after)
                    
                    else:
                        logger.error(f"HTTP {response.status} for {url}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)
            
            except asyncio.TimeoutError:
                logger.error(f"Download timeout for {output_path.name}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
            
            except Exception as e:
                logger.error(f"Download failed: {str(e)}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
        
        return False
    
    async def download_batch(self, file_urls: List[Dict[str, str]], 
                            skip_existing: bool = True) -> List[Path]:
        """
        Download multiple files in parallel.
        
        Args:
            file_urls: List of dicts with 'url' and 'filename' keys
            skip_existing: Skip files that already exist locally
        
        Returns:
            List of successfully downloaded file paths
        """
        connector = aiohttp.TCPConnector(limit=self.concurrent_downloads)
        
        async with aiohttp.ClientSession(
            connector=connector,
            headers={"Authorization": f"Bearer {self.api_key}"}
        ) as session:
            tasks = []
            
            for file_info in file_urls:
                url = file_info['url']
                filename = file_info['filename']
                output_path = self.download_dir / filename
                
                if skip_existing and output_path.exists():
                    logger.info(f"Skipping existing file: {filename}")
                    continue
                
                task = self.download_file(session, url, output_path)
                tasks.append((task, output_path))
            
            if not tasks:
                logger.info("No files to download")
                return []
            
            logger.info(f"Downloading {len(tasks)} files with {self.concurrent_downloads} concurrent connections")
            
            results = await asyncio.gather(*[task for task, _ in tasks])
            successful_paths = [path for (task, path), success in zip(tasks, results) if success]
            
            logger.info(f"Download complete: {len(successful_paths)}/{len(tasks)} successful")
            return successful_paths
    
    def verify_file_integrity(self, filepath: Path, expected_sha256: Optional[str] = None) -> bool:
        """
        Verify file integrity with SHA256 checksum.
        
        Args:
            filepath: Path to file
            expected_sha256: Expected SHA256 hash (optional)
        
        Returns:
            True if file is valid
        """
        if not filepath.exists():
            logger.error(f"File not found: {filepath}")
            return False
        
        if not expected_sha256:
            # Just check existence
            return True
        
        logger.info(f"Verifying {filepath.name}...")
        sha256 = hashlib.sha256()
        
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha256.update(chunk)
        
        calculated_hash = sha256.hexdigest()
        is_valid = calculated_hash == expected_sha256
        
        if is_valid:
            logger.info(f"✓ Integrity verified for {filepath.name}")
        else:
            logger.error(f"✗ Integrity check failed for {filepath.name}")
            logger.error(f"  Expected: {expected_sha256}")
            logger.error(f"  Got: {calculated_hash}")
        
        return is_valid
    
    def list_downloaded_files(self, pattern: str = "*") -> List[Path]:
        """
        List files in download directory.
        
        Args:
            pattern: Glob pattern (e.g., "*.gz", "us_stocks_*")
        
        Returns:
            List of file paths
        """
        files = list(self.download_dir.glob(pattern))
        logger.info(f"Found {len(files)} files matching '{pattern}'")
        return files
    
    def generate_flat_file_urls(self, date: str, market: str = "us_stocks_sip") -> List[Dict[str, str]]:
        """
        Generate Polygon.io flat file URLs for a specific date.
        
        Note: This is an example structure. Actual URLs may vary.
        Check Polygon.io documentation for current flat file structure.
        
        Args:
            date: Date in YYYY-MM-DD format
            market: Market type (us_stocks_sip, etc.)
        
        Returns:
            List of file info dicts
        """
        # Example structure - adjust based on actual Polygon.io flat file format
        base_url = f"https://files.polygon.io/flat-files/{market}/{date}"
        
        file_types = [
            "trades",
            "quotes",
            "aggregates_minute",
            "aggregates_second"
        ]
        
        files = []
        for file_type in file_types:
            filename = f"{date}_{market}_{file_type}.csv.gz"
            url = f"{base_url}/{filename}"
            files.append({
                'url': url,
                'filename': filename,
                'date': date,
                'type': file_type
            })
        
        return files
    
    def generate_date_range_urls(self, start_date: str, end_date: str, 
                                 market: str = "us_stocks_sip") -> List[Dict[str, str]]:
        """
        Generate flat file URLs for a date range.
        
        Args:
            start_date: Start date YYYY-MM-DD
            end_date: End date YYYY-MM-DD
            market: Market type
        
        Returns:
            List of all file info dicts for date range
        """
        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')
        
        all_files = []
        current = start
        
        while current <= end:
            date_str = current.strftime('%Y-%m-%d')
            files = self.generate_flat_file_urls(date_str, market)
            all_files.extend(files)
            current += timedelta(days=1)
        
        logger.info(f"Generated {len(all_files)} file URLs for {start_date} to {end_date}")
        return all_files


def download_flat_files(api_key: str, file_urls: List[Dict[str, str]], 
                       download_dir: str = "./data/flat_files") -> List[Path]:
    """
    Convenience function to download flat files synchronously.
    
    Args:
        api_key: Polygon.io API key
        file_urls: List of file info dicts
        download_dir: Download directory
    
    Returns:
        List of downloaded file paths
    """
    downloader = FlatFileDownloader(api_key, download_dir)
    return asyncio.run(downloader.download_batch(file_urls))


if __name__ == "__main__":
    # Test the downloader
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    api_key = os.getenv("POLYGON_API_KEY")
    
    if not api_key:
        print("ERROR: POLYGON_API_KEY not found in .env file")
        exit(1)
    
    logging.basicConfig(level=logging.INFO)
    
    # Initialize downloader
    downloader = FlatFileDownloader(api_key, download_dir="./data/flat_files")
    
    print("\n=== Flat File Downloader Test ===")
    print(f"Download directory: {downloader.download_dir}")
    
    # Generate example URLs (adjust for actual Polygon.io structure)
    print("\n=== Generating example file URLs ===")
    file_urls = downloader.generate_flat_file_urls("2024-10-01")
    for file_info in file_urls:
        print(f"  {file_info['filename']}")
    
    # List existing files
    print("\n=== Listing existing downloaded files ===")
    existing_files = downloader.list_downloaded_files("*.gz")
    if existing_files:
        for file_path in existing_files:
            size_mb = file_path.stat().st_size / (1024 * 1024)
            print(f"  {file_path.name} ({size_mb:.2f} MB)")
    else:
        print("  No files found")
    
    print("\n✓ Flat file downloader test complete!")
    print("\nNote: To actually download files, ensure the URLs match Polygon.io's")
    print("      current flat file structure and uncomment the download code.")
