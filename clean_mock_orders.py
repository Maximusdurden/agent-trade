"""
Download trading_agent.db from GCS, delete all mock-order trades, and re-upload.
"""
import os
import sys
import sqlite3
import tempfile
from google.cloud import storage

BUCKET_NAME = "agenttrade-us-data-bucket"
BLOB_PATH = "trading_agent.db"

def main():
    client = storage.Client(project="agenttrade-us")
    bucket = client.bucket(BUCKET_NAME)
    blob = bucket.blob(BLOB_PATH)

    # Download to temp file
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp_path = tmp.name
    tmp.close()
    blob.download_to_filename(tmp_path)
    print(f"Downloaded {BLOB_PATH} to {tmp_path}")

    # Connect and clean
    conn = sqlite3.connect(tmp_path)
    cursor = conn.cursor()

    # Check what's in the trades table
    cursor.execute("SELECT COUNT(*) FROM trades")
    total = cursor.fetchone()[0]
    print(f"Total trades in DB: {total}")

    cursor.execute("SELECT COUNT(*) FROM trades WHERE alpaca_order_id LIKE 'mock-order-%'")
    mock_count = cursor.fetchone()[0]
    print(f"Mock-order trades: {mock_count}")

    if mock_count > 0:
        cursor.execute("DELETE FROM trades WHERE alpaca_order_id LIKE 'mock-order-%'")
        conn.commit()
        print(f"Deleted {mock_count} mock-order trades")

    # Also check remaining trades
    cursor.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 15")
    rows = cursor.fetchall()
    print(f"\nRemaining trades (up to 15):")
    for r in rows:
        print(f"  id={r[0]}, order_id={r[2]}, symbol={r[4]}, side={r[5]}, status={r[8]}")

    conn.close()

    # Upload back
    blob.upload_from_filename(tmp_path)
    print(f"\nUploaded cleaned DB back to gs://{BUCKET_NAME}/{BLOB_PATH}")

    # Cleanup
    os.unlink(tmp_path)

if __name__ == "__main__":
    main()