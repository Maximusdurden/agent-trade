import os
import sys
import logging
import subprocess
from core import config

logger = logging.getLogger("GCSSync")

def get_gcs_client():
    """
    Returns a google.cloud.storage.Client instance.
    First tries service account credentials from GOOGLE_APPLICATION_CREDENTIALS.
    Falls back to default authentication (works on Cloud Run / environments with ADC).
    """
    from google.cloud import storage
    try:
        # Try service account credentials first
        creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if creds_path and os.path.exists(creds_path):
            client = storage.Client.from_service_account_json(creds_path)
            return client

        # Fallback to default credentials
        client = storage.Client()
        return client
    except Exception as e:
        raise RuntimeError(f"Failed to authenticate with GCS. Please ensure GOOGLE_APPLICATION_CREDENTIALS is set correctly. Error details: {e}")

def download_from_gcs():
    """
    Downloads the database and DoD balances from Google Cloud Storage
    to their configured local paths before runner execution.
    """
    gcs_bucket = os.getenv("GCS_BUCKET_NAME")
    if not gcs_bucket:
        logger.info("GCS_BUCKET_NAME not set. Skipping download from GCS.")
        return

    try:
        logger.info(f"Downloading database and balances from gs://{gcs_bucket}...")
        client = get_gcs_client()
        bucket = client.bucket(gcs_bucket)

        # 1. Download database
        db_path = str(config.DATABASE_PATH)
        blob = bucket.blob("trading_agent.db")
        if blob.exists():
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            blob.download_to_filename(db_path)
            logger.info(f"Successfully synchronized {db_path} from GCS.")
        else:
            logger.info("No database file found on GCS.")

        # 2. Download portfolio DoD balances CSV
        csv_path = "portfolio_dod_balances.csv"
        blob_csv = bucket.blob("portfolio_dod_balances.csv")
        if blob_csv.exists():
            blob_csv.download_to_filename(csv_path)
            logger.info(f"Successfully synchronized {csv_path} from GCS.")
        else:
            logger.info("No DoD balances CSV file found on GCS.")

    except Exception as e:
        logger.error(f"Failed to download files from GCS: {e}")

def upload_to_gcs():
    """
    Uploads the local database, trading log, and DoD balances to Google Cloud Storage
    if GCS_BUCKET_NAME is configured.
    """
    gcs_bucket = os.getenv("GCS_BUCKET_NAME")
    if not gcs_bucket:
        logger.info("GCS_BUCKET_NAME not set. Skipping upload to GCS.")
        return

    try:
        logger.info(f"Uploading database and logs to gs://{gcs_bucket}...")
        client = get_gcs_client()
        bucket = client.bucket(gcs_bucket)

        # 1. Upload database
        db_path = str(config.DATABASE_PATH)
        if os.path.exists(db_path):
            blob = bucket.blob("trading_agent.db")
            blob.upload_from_filename(db_path)
            logger.info(f"Successfully uploaded {db_path} to GCS.")
        else:
            logger.warning(f"Database file not found at {db_path}.")

        # 2. Upload trading log
        log_path = str(config.LOG_FILE)
        if os.path.exists(log_path):
            blob = bucket.blob("trading.log")
            blob.upload_from_filename(log_path)
            logger.info(f"Successfully uploaded {log_path} to GCS.")
        else:
            logger.warning(f"Log file not found at {log_path}.")

        # 3. Upload portfolio DoD balances CSV
        csv_path = "portfolio_dod_balances.csv"
        if os.path.exists(csv_path):
            blob = bucket.blob("portfolio_dod_balances.csv")
            blob.upload_from_filename(csv_path)
            logger.info(f"Successfully uploaded {csv_path} to GCS.")
        else:
            logger.warning(f"DoD balances CSV not found at {csv_path}.")

    except Exception as e:
        logger.error(f"Failed to upload files to GCS: {e}")

def check_kill_switch() -> dict:
    """
    Downloads kill_switch.json from GCS, parses it, and returns the status.
    Fallback to a local copy if GCS is down.
    """
    import json
    gcs_bucket = os.getenv("GCS_BUCKET_NAME")
    local_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kill_switch.json")
    
    if not gcs_bucket:
        # Fallback to local if no GCS bucket
        if os.path.exists(local_path):
            try:
                with open(local_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"status": "ACTIVE", "updated_at": "", "updated_by": "default"}

    try:
        client = get_gcs_client()
        bucket = client.bucket(gcs_bucket)
        blob = bucket.blob("kill_switch.json")
        if blob.exists():
            data_str = blob.download_as_text()
            data = json.loads(data_str)
            # Sync to local cache
            try:
                with open(local_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass
            return data
    except Exception as e:
        logger.warning(f"Error checking GCS kill switch: {e}. Falling back to local cache.")
        if os.path.exists(local_path):
            try:
                with open(local_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
                
    return {"status": "ACTIVE", "updated_at": "", "updated_by": "default"}

def set_kill_switch_state(status: str, updated_by: str = "system") -> bool:
    """
    Sets the kill switch status on GCS (and writes local cache).
    """
    import json
    from datetime import datetime
    data = {
        "status": status.upper(),
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "updated_by": updated_by
    }
    local_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kill_switch.json")
    
    # Save local cache
    try:
        with open(local_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to write local kill switch cache: {e}")

    gcs_bucket = os.getenv("GCS_BUCKET_NAME")
    if not gcs_bucket:
        logger.warning("GCS_BUCKET_NAME not set. Saved kill switch locally only.")
        return True

    try:
        client = get_gcs_client()
        bucket = client.bucket(gcs_bucket)
        blob = bucket.blob("kill_switch.json")
        blob.upload_from_string(json.dumps(data, indent=2), content_type="application/json")
        logger.info(f"Successfully uploaded kill_switch.json with status {status} to GCS.")
        return True
    except Exception as e:
        logger.error(f"Failed to upload kill_switch.json to GCS: {e}")
        return False

