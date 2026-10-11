import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from google.cloud.storage.exceptions import DataCorruption

from core import config
from core.gcs_sync import download_from_gcs, is_gcs_checksum_mismatch, upload_to_gcs


class TestGCSChecksumHandling(unittest.TestCase):
    def test_identifies_storage_data_corruption(self):
        self.assertTrue(is_gcs_checksum_mismatch(DataCorruption(None, "checksum mismatch")))
        self.assertFalse(is_gcs_checksum_mismatch(RuntimeError("unrelated failure")))

    def test_corrupt_download_does_not_replace_local_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trading_agent.db")
            with open(db_path, "wb") as db_file:
                db_file.write(b"known-good database")

            blob = MagicMock()
            blob.exists.return_value = True
            blob.download_to_filename.side_effect = DataCorruption(None, "checksum mismatch")
            client = MagicMock()
            client.bucket.return_value.blob.return_value = blob

            with patch.dict(os.environ, {"GCS_BUCKET_NAME": "test-bucket"}), \
                    patch.object(config, "DATABASE_PATH", db_path), \
                    patch("core.gcs_sync.get_gcs_client", return_value=client):
                download_from_gcs()

            with open(db_path, "rb") as db_file:
                self.assertEqual(db_file.read(), b"known-good database")
            self.assertFalse(os.path.exists(db_path + ".tmp"))

    def test_corrupt_merge_download_does_not_upload_stale_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trading_agent.db")
            with open(db_path, "wb") as db_file:
                db_file.write(b"local database")

            blob = MagicMock()
            blob.download_to_filename.side_effect = DataCorruption(None, "checksum mismatch")
            client = MagicMock()
            client.bucket.return_value.blob.return_value = blob

            with patch.dict(os.environ, {"GCS_BUCKET_NAME": "test-bucket"}), \
                    patch.object(config, "DATABASE_PATH", db_path), \
                    patch("core.gcs_sync.get_gcs_client", return_value=client), \
                    patch("core.gcs_sync.build_dod_csv"):
                upload_to_gcs()

            blob.upload_from_filename.assert_not_called()
            self.assertFalse(os.path.exists(db_path + ".merged"))


if __name__ == "__main__":
    unittest.main()
