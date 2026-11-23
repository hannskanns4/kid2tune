import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch


if os.name != "nt":
    from app import play_history, sync_manager


@unittest.skipIf(os.name == "nt", "file locking uses Linux fcntl")
class FileLockingTests(unittest.TestCase):
    def test_concurrent_history_writes_keep_all_distinct_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            history_path = os.path.join(temp_dir, "history.json")
            with patch.object(play_history, "HISTORY_PATH", history_path), \
                    patch.object(play_history, "_LOCK_PATH", history_path + ".lock"), \
                    patch.object(play_history, "MAX_ENTRIES", 20), \
                    patch.object(play_history, "_fetch_metadata_later"):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(
                        lambda index: play_history.record_play(
                            "url", f"https://example.test/{index}", f"Item {index}",
                        ),
                        range(16),
                    ))
                values = {entry["value"] for entry in play_history.get_history()}
                self.assertEqual(len(values), 16)

    def test_concurrent_sync_queue_updates_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            queue_path = os.path.join(temp_dir, "pending.json")
            with patch.object(sync_manager, "PENDING_PATH", queue_path), \
                    patch.object(
                        sync_manager, "PENDING_LOCK_PATH", queue_path + ".lock",
                    ), \
                    patch.object(
                        sync_manager, "get_sync_config", return_value={"enabled": True},
                    ), \
                    patch.object(sync_manager, "_box_id", return_value="test-box"):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(
                        lambda index: sync_manager.queue_change(
                            "upsert", f"UID{index}", {"value": str(index)},
                        ),
                        range(16),
                    ))
                with open(queue_path, encoding="utf-8") as queue_file:
                    entries = json.load(queue_file)
                self.assertEqual(len(entries), 16)


if __name__ == "__main__":
    unittest.main()
