import subprocess
import csv
from pathlib import Path
from datetime import datetime, timedelta

S3_ENDPOINT = "https://files.polygon.io"
BUCKET = "flatfiles"
YEAR = 2025
STOCK_PREFIX = "us_stocks_sip/minute_aggs_v1"
CRYPTO_PREFIX = "global_crypto/minute_aggs_v1"
LOCAL_TMP = Path("./tmp")
LOCAL_TMP.mkdir(exist_ok=True)

def get_first_tuesday(year):
    date = datetime(year, 1, 1)
    while date.weekday() != 1:  # Monday=0, Tuesday=1
        date += timedelta(days=1)
    return date

def download_and_extract_file(s3_prefix, date):
    date_str = date.strftime("%Y-%m-%d")
    filename = f"{date_str}.csv.gz"
    s3_path = f"s3://{BUCKET}/{s3_prefix}/{date.year}/{date.month:02d}/{filename}"
    print(f"Downloading: {s3_path}")
    run_command(f"aws s3 cp {s3_path} {LOCAL_TMP} --endpoint-url {S3_ENDPOINT}")
    gz_path = LOCAL_TMP / filename
    if gz_path.exists():
        run_command(f"gzip -d -f {gz_path}")
    else:
        print(f"✗ Missing: {gz_path}")

def run_command(cmd):
    try:
        subprocess.run(cmd, shell=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {e}")

def collect_tickers(file_path):
    tickers = set()
    try:
        with open(file_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                tickers.add(row["ticker"])
    except Exception as e:
        print(f"⚠️ Failed to read {file_path}: {e}")
    return tickers

def main():
    tuesday = get_first_tuesday(YEAR)

    print("=== STOCK FILE ===")
    download_and_extract_file(STOCK_PREFIX, tuesday)

    print("=== CRYPTO FILE ===")
    download_and_extract_file(CRYPTO_PREFIX, tuesday)

    stock_file = LOCAL_TMP / f"{tuesday.strftime('%Y-%m-%d')}.csv"
    crypto_file = LOCAL_TMP / f"{tuesday.strftime('%Y-%m-%d')}.csv"

    stock_tickers = collect_tickers(stock_file)
    crypto_tickers = collect_tickers(crypto_file)

    print(f"✓ Stock tickers: {len(stock_tickers)}")
    print(f"✓ Crypto tickers: {len(crypto_tickers)}")

    overlap = stock_tickers & crypto_tickers
    if overlap:
        print(f"⚠️ Overlapping tickers: {len(overlap)}")
        for t in sorted(overlap):
            print(f" - {t}")
    else:
        print("✓ No overlapping tickers.")

if __name__ == "__main__":
    main()
