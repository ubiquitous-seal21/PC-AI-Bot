import importlib.util
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


def load_engine():
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="pcaibot-test-")
    spec = importlib.util.spec_from_file_location(
        "hub_engine", os.path.join(os.path.dirname(__file__), "..", "ai", "hub", "engine.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EngineSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = load_engine()
        cls.e.PC_USER = "test-user"

    def test_magic_packet_accepts_only_a_valid_mac(self):
        self.e.PC_MAC = "AA:BB:CC:DD:EE:FF"
        packet = self.e.magic_packet()
        self.assertEqual(len(packet), 102)
        self.e.PC_MAC = "not-a-mac"
        with self.assertRaises(ValueError):
            self.e.magic_packet()

    def test_active_session_detection_blocks_shutdown_for_active_session(self):
        with patch.object(self.e, "run", return_value=type("P", (), {
            "returncode": 0, "stdout": "Active=yes\nState=active\n", "stderr": ""
        })):
            self.assertTrue(self.e.pc_has_active_sessions())

    def test_active_session_detection_allows_no_sessions(self):
        with patch.object(self.e, "run", return_value=type("P", (), {
            "returncode": 0, "stdout": "", "stderr": ""
        })):
            self.assertFalse(self.e.pc_has_active_sessions())

    def test_active_session_check_fails_closed_on_ssh_error(self):
        with patch.object(self.e, "run", return_value=type("P", (), {
            "returncode": 255, "stdout": "", "stderr": "offline"
        })):
            self.assertTrue(self.e.pc_has_active_sessions())

    def test_pause_blocks_chat_and_worker_busy_blocks_chat(self):
        self.e.state["worker_busy"] = True
        self.e.state["paused"] = False
        with self.assertRaises(RuntimeError):
            self.e.enqueue_chat("hello")
        self.e.state["worker_busy"] = False
        self.e.state["paused"] = True
        with self.assertRaises(RuntimeError):
            self.e.enqueue_chat("hello")
        self.e.state["paused"] = False

    def test_clean_briefing_rejects_bad_values(self):
        settings = self.e.clean_briefing({
            "deliver_time": "99:99",
            "prep_minutes": -20,
            "lookback_hours": 999,
            "top_n": 0,
            "min_importance": 99,
            "max_items": 1,
            "focuses": [{"name": "", "description": "empty", "count": 4}],
        })
        self.assertEqual(settings["deliver_time"], "07:00")
        self.assertEqual(settings["prep_minutes"], 10)
        self.assertEqual(settings["lookback_hours"], 72)
        self.assertEqual(settings["top_n"], 1)
        self.assertEqual(settings["min_importance"], 10)
        self.assertEqual(settings["max_items"], 20)
        self.assertEqual(settings["focuses"], [])

    def test_clean_briefing_preserves_focus_order_and_removes_duplicates(self):
        settings = self.e.clean_briefing({
            "focuses": [
                {"name": "Science", "description": "Research", "count": 3},
                {"name": "Technology", "description": "AI and hardware", "count": 5},
                {"name": "science", "description": "Duplicate", "count": 2},
            ],
        })
        self.assertEqual([f["name"] for f in settings["focuses"]], ["Science", "Technology"])

    def test_cluster_merges_distinctive_headlines(self):
        items = [
            {"id": "1", "title": "Major lunar mission reaches orbit", "text": "long text", "source": "A"},
            {"id": "2", "title": "Lunar mission reaches orbit after launch", "text": "more text", "source": "B"},
            {"id": "3", "title": "Local recipe contest opens entries", "text": "short", "source": "C"},
        ]
        groups = self.e.cluster(items)
        self.assertEqual(len(groups), 2)

    def test_url_validation_rejects_loopback_and_private_hosts(self):
        with self.assertRaises(RuntimeError):
            self.e.validate_public_url("http://127.0.0.1/admin")
        with self.assertRaises(RuntimeError):
            self.e.validate_public_url("http://169.254.169.254/latest/meta-data")
        with self.assertRaises(RuntimeError):
            self.e.validate_public_url("file:///etc/passwd")

    def test_pause_toggle_is_persistent_in_process(self):
        self.assertTrue(self.e.set_paused(True))
        self.assertFalse(self.e.set_paused(False))


if __name__ == "__main__":
    unittest.main()
