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

    def test_magic_packet_is_sent_to_configured_and_limited_broadcast(self):
        original_mac = self.e.PC_MAC
        original_broadcast = self.e.LAN_BROADCAST
        try:
            self.e.PC_MAC = "AA:BB:CC:DD:EE:FF"
            self.e.LAN_BROADCAST = "192.168.1.255"
            with patch.object(self.e.socket, "socket") as socket_factory, patch.object(
                self.e.os.path, "exists", return_value=False
            ):
                sock = socket_factory.return_value
                self.e.send_magic()
                sent_addresses = [call.args[1][0] for call in sock.sendto.call_args_list]
                self.assertIn("192.168.1.255", sent_addresses)
                self.assertIn("255.255.255.255", sent_addresses)
                sock.close.assert_called_once()
        finally:
            self.e.PC_MAC = original_mac
            self.e.LAN_BROADCAST = original_broadcast

    def test_wake_does_not_send_magic_packet_when_pc_is_already_reachable(self):
        with patch.object(self.e, "ollama_up", return_value=False), patch.object(
            self.e, "pc_host_up", return_value=True
        ), patch.object(self.e, "send_magic") as send_magic:
            self.assertEqual(self.e.wake_pc(), {
                "sent": False, "pc_on": True, "ollama_up": False
            })
            send_magic.assert_not_called()

    def test_ensure_pc_does_not_mark_an_already_on_pc_as_woken_by_hub(self):
        with patch.object(self.e, "ollama_up", side_effect=[False, True]), patch.object(
            self.e, "pc_host_up", return_value=True
        ), patch.object(self.e, "send_magic") as send_magic, patch.object(
            self.e.time, "sleep"
        ):
            self.e.state["we_woke"] = False
            self.e.ensure_pc(timeout=1)
            send_magic.assert_not_called()
            self.assertFalse(self.e.state["we_woke"])

    def test_ensure_pc_reports_when_online_host_has_no_ollama(self):
        with patch.object(self.e, "ollama_up", return_value=False), patch.object(
            self.e, "pc_host_up", return_value=True
        ), patch.object(self.e, "send_magic") as send_magic:
            with self.assertRaisesRegex(RuntimeError, "PC is on, but Ollama"):
                self.e.ensure_pc(timeout=0)
            send_magic.assert_not_called()

    def test_active_session_detection_blocks_shutdown_for_active_session(self):
        with patch.object(self.e, "run", return_value=type("P", (), {
            "returncode": 0, "stdout": "SessionActive=yes\n", "stderr": ""
        })):
            self.assertTrue(self.e.pc_has_active_sessions())

    def test_active_session_detection_allows_no_sessions(self):
        with patch.object(self.e, "run", return_value=type("P", (), {
            "returncode": 0, "stdout": "", "stderr": ""
        })):
            self.assertFalse(self.e.pc_has_active_sessions())

    def test_locked_kde_lockscreen_does_not_block_shutdown(self):
        with patch.object(self.e, "run", return_value=type("P", (), {
            "returncode": 0,
            "stdout": "Active=yes State=active LockedHint=yes IdleHint=no Type=x11 Class=user\n",
            "stderr": ""
        })):
            self.assertFalse(self.e.pc_has_active_sessions())

    def test_greeter_session_does_not_block_shutdown(self):
        with patch.object(self.e, "run", return_value=type("P", (), {
            "returncode": 0,
            "stdout": "Active=yes State=active LockedHint=no IdleHint=no Type=greeter Class=greeter\n",
            "stderr": ""
        })):
            self.assertFalse(self.e.pc_has_active_sessions())

    def test_kde_screenlocker_process_allows_shutdown(self):
        with patch.object(self.e, "run", return_value=type("P", (), {
            "returncode": 0,
            "stdout": "Active=yes State=active LockedHint=no IdleHint=no Type=x11 Class=user\nLockedDesktop=yes\n",
            "stderr": ""
        })):
            self.assertFalse(self.e.pc_has_active_sessions())

    def test_session_check_inspects_kde_locker_executable(self):
        with patch.object(self.e, "run", return_value=type("P", (), {
            "returncode": 0, "stdout": "", "stderr": ""
        })) as run:
            self.e.pc_has_active_sessions()
            remote_command = " ".join(run.call_args.args[0])
            self.assertIn("/proc/[0-9]*/exe", remote_command)
            self.assertIn("kscreenlocker_greet", remote_command)

    def test_remote_session_is_not_ignored(self):
        with patch.object(self.e, "run", return_value=type("P", (), {
            "returncode": 0, "stdout": "", "stderr": ""
        })) as run:
            self.e.pc_has_active_sessions()
            remote_command = " ".join(run.call_args.args[0])
            self.assertIn("remote=$(printf", remote_command)
            self.assertIn("[ \"$locked\" != yes ]", remote_command)

    def test_active_session_check_fails_closed_on_ssh_error(self):
        with patch.object(self.e, "run", return_value=type("P", (), {
            "returncode": 255, "stdout": "", "stderr": "offline"
        })):
            self.assertTrue(self.e.pc_has_active_sessions())

    def test_power_off_requires_pc_address_and_user(self):
        original_ip = self.e.PC_IP
        original_user = self.e.PC_USER
        try:
            self.e.PC_IP = ""
            self.e.PC_USER = "test-user"
            self.assertFalse(self.e.power_off_pc())
        finally:
            self.e.PC_IP = original_ip
            self.e.PC_USER = original_user

    def test_power_off_sends_remote_command_as_separate_arguments(self):
        original_ip = self.e.PC_IP
        original_cmd = self.e.PC_OFF_CMD
        try:
            self.e.PC_IP = "192.168.1.100"
            self.e.PC_OFF_CMD = "sudo systemctl poweroff"
            with patch.object(self.e, "run", return_value=type("P", (), {
                "returncode": 0, "stdout": "", "stderr": ""
            })) as run:
                self.assertTrue(self.e.power_off_pc())
                command = run.call_args.args[0]
                self.assertEqual(command[-3:], ["sudo", "systemctl", "poweroff"])
        finally:
            self.e.PC_IP = original_ip
            self.e.PC_OFF_CMD = original_cmd

    def test_automatic_poweroff_rechecks_sessions_in_one_remote_command(self):
        original_ip = self.e.PC_IP
        original_user = self.e.PC_USER
        original_cmd = self.e.PC_OFF_CMD
        try:
            self.e.PC_IP = "192.168.1.100"
            self.e.PC_USER = "test-user"
            self.e.PC_OFF_CMD = "sudo systemctl poweroff"
            with patch.object(self.e, "run", return_value=type("P", (), {
                "returncode": 3, "stdout": "SessionActive=yes\n", "stderr": ""
            })) as run:
                self.assertFalse(self.e.power_off_pc_if_idle())
                remote_script = run.call_args.args[0][-1]
                self.assertIn("loginctl list-sessions", remote_script)
                self.assertIn("exec sudo systemctl poweroff", remote_script)
                self.assertIn("exit 3", remote_script)
        finally:
            self.e.PC_IP = original_ip
            self.e.PC_USER = original_user
            self.e.PC_OFF_CMD = original_cmd

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

    def test_cancel_briefing_prevents_late_worker_updates(self):
        rid = self.e.new_run("briefing", "Briefing")
        self.e.cancel_briefing(rid)
        self.e.update_briefing(rid, status="ready", output="late result")
        run = self.e.get_run(rid)
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(run["detail"], "Cancelled by user")

    def test_cancelled_queued_briefing_is_skipped_by_worker(self):
        rid = self.e.new_run("briefing", "Briefing")
        self.e.cancel_briefing(rid)
        with patch.object(self.e, "fresh_items", side_effect=AssertionError("should not fetch")):
            self.e.build_briefing(rid)
        self.assertEqual(self.e.get_run(rid)["status"], "cancelled")

    def test_cancel_briefing_rejects_completed_runs(self):
        rid = self.e.new_run("briefing", "Briefing")
        self.e.update_briefing(rid, status="ready")
        with self.assertRaises(RuntimeError):
            self.e.cancel_briefing(rid)

    def test_cancel_briefing_is_account_scoped(self):
        account = self.e.create_account("Briefing owner", "briefing-password")
        rid = self.e.new_run("briefing", "Briefing", account_id=account["id"])
        with self.assertRaises(LookupError):
            self.e.cancel_briefing(rid, account_id=account["id"] + 1)
        self.assertEqual(self.e.get_run(rid)["status"], "queued")

    def test_accounts_are_separate_and_passwords_are_verified_by_hash(self):
        first = self.e.create_account("Alice", "alice-password")
        second = self.e.create_account("Bob", "bob-password")
        self.assertIsNotNone(self.e.verify_account("Alice", "alice-password"))
        self.assertIsNone(self.e.verify_account("Alice", "wrong-password"))
        self.e.save_general({"recipients": "alice@example.com"}, first["id"])
        self.e.save_general({"recipients": "bob@example.com"}, second["id"])
        self.assertEqual(self.e.get_recipients(first["id"]), ["alice@example.com"])
        self.assertEqual(self.e.get_recipients(second["id"]), ["bob@example.com"])


if __name__ == "__main__":
    unittest.main()
