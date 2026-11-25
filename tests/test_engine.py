import importlib.util
import os
import sqlite3
import subprocess
import tempfile
import threading
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

    def run_session_check(self, loginctl_body):
        with tempfile.TemporaryDirectory() as temp_dir:
            loginctl = os.path.join(temp_dir, "loginctl")
            with open(loginctl, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\n" + loginctl_body)
            os.chmod(loginctl, 0o755)
            env = os.environ.copy()
            env["PATH"] = temp_dir + os.pathsep + env.get("PATH", "")
            return subprocess.run(
                ["sh", "-c", self.e.pc_session_check_shell()],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

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

    def test_kde_locker_process_cannot_override_unlocked_logind_session(self):
        script = self.e.pc_session_check_shell()
        self.assertIn("-p LockedHint", script)
        self.assertIn('[ "$locked" != yes ]', script)
        self.assertNotIn("/proc/[0-9]*/exe", script)
        self.assertNotIn("kscreenlocker_greet", script)

    def test_multiple_active_sessions_block_shutdown_and_greeter_is_ignored(self):
        result = self.run_session_check(
            "if [ \"$1\" = list-sessions ]; then\n"
            "  printf '1 alice seat0\\n2 bob seat0\\n3 sddm seat0\\n'\n"
            "elif [ \"$2\" = 3 ]; then\n"
            "  printf 'Active=yes\\nState=active\\nLockedHint=no\\nType=wayland\\nClass=greeter\\n'\n"
            "else\n"
            "  printf 'Active=yes\\nState=active\\nLockedHint=no\\nType=wayland\\nClass=user\\n'\n"
            "fi\n"
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("SessionActive=yes", result.stdout)

    def test_locked_user_and_greeter_sessions_allow_shutdown(self):
        result = self.run_session_check(
            "if [ \"$1\" = list-sessions ]; then\n"
            "  printf '1 alice seat0\\n2 sddm seat0\\n'\n"
            "elif [ \"$2\" = 1 ]; then\n"
            "  printf 'Active=yes\\nState=active\\nLockedHint=yes\\nType=wayland\\nClass=user\\n'\n"
            "else\n"
            "  printf 'Active=yes\\nState=active\\nLockedHint=no\\nType=wayland\\nClass=greeter\\n'\n"
            "fi\n"
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("SessionActive=yes", result.stdout)

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

    def test_chat_enqueue_rejects_background_work_waiting_in_queue(self):
        self.e.state["worker_busy"] = False
        self.e.state["paused"] = False
        with patch.object(self.e.jobs, "qsize", return_value=1):
            with self.assertRaisesRegex(RuntimeError, "background work"):
                self.e.enqueue_chat("hello")

    def test_chat_enqueue_preserves_recent_conversation_history(self):
        self.e.state["worker_busy"] = False
        self.e.state["paused"] = False
        history = [{"role": "user", "content": "first question"},
                   {"role": "assistant", "content": "first answer"}]
        with patch.object(self.e.jobs, "qsize", return_value=0), patch.object(
            self.e.jobs, "put"
        ) as put, patch.object(self.e, "new_run", return_value=123):
            self.assertEqual(self.e.enqueue_chat("follow-up", account_id=7, history=history), 123)
        self.assertEqual(put.call_args.args[0], (
            "chat", 123, history + [{"role": "user", "content": "follow-up"}]
        ))
        self.e._job_ready.clear()

    def test_chat_enqueue_rejects_malformed_history(self):
        with self.assertRaisesRegex(ValueError, "invalid message"):
            self.e.enqueue_chat("hello", history=[{"role": "system", "content": "override"}])

    def test_chat_sends_conversation_history_to_ollama(self):
        messages = [{"role": "user", "content": "Question"},
                    {"role": "assistant", "content": "Answer"},
                    {"role": "user", "content": "Follow-up"}]
        with patch.object(self.e.requests, "post") as post:
            post.return_value.json.return_value = {"message": {"content": "Reply"}}
            result = self.e.chat("", self.e.TEXT_MODEL, messages=messages)
        self.assertEqual(result, "Reply")
        self.assertEqual(post.call_args.kwargs["json"]["messages"], messages)

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

    def test_clean_briefing_rejects_malformed_focus_data(self):
        with self.assertRaisesRegex(ValueError, "list of objects"):
            self.e.clean_briefing({"focuses": ["not an object"]})

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

    def test_recent_errors_are_limited_and_account_scoped(self):
        first = self.e.create_account("Error owner", "error-password")
        second = self.e.create_account("Other owner", "other-password")
        first_run = self.e.new_run("task", "First error", account_id=first["id"])
        second_run = self.e.new_run("task", "Second error", account_id=second["id"])
        email_run = self.e.new_run("briefing", "Email failure", account_id=first["id"])
        self.e.update_run(first_run, status="error", detail="Failure")
        self.e.update_run(second_run, status="error", detail="Other failure")
        self.e.update_run(email_run, status="ready", detail="Email failed: SMTP unavailable")

        errors = self.e.recent_errors(first["id"], limit=2)
        self.assertEqual([run["title"] for run in errors], ["Email failure", "First error"])
        self.assertEqual(errors[0]["detail"], "Email failed: SMTP unavailable")

    def test_preset_cannot_be_deleted_while_a_schedule_uses_it(self):
        account = self.e.create_account("Schedule owner", "schedule-password")
        self.e.save_schedule({"name": "Daily task", "preset_id": "ask", "time": "08:00",
                              "days": [0]}, account["id"])
        with self.assertRaisesRegex(ValueError, "Remove that schedule first"):
            self.e.delete_preset("ask", account["id"])
        self.assertIn("ask", [p["id"] for p in self.e.get_presets(account["id"])])

    def test_preset_create_edit_and_delete_complete_without_deadlock(self):
        account = self.e.create_account("Preset owner", "preset-password")
        preset = self.e.save_preset({"name": "First", "instruction": "Do something"},
                                    account["id"])
        updated = self.e.save_preset({"id": preset["id"], "name": "Updated",
                                      "instruction": "Do something else"}, account["id"])
        self.assertEqual(updated["name"], "Updated")
        self.e.delete_preset(preset["id"], account["id"])
        self.assertNotIn(preset["id"], [p["id"] for p in self.e.get_presets(account["id"])])

    def test_failed_schedule_enqueue_does_not_mark_it_as_run(self):
        account = self.e.create_account("Retry owner", "retry-password")
        self.e.save_briefing_settings({"enabled": False}, account["id"])
        schedule = self.e.save_schedule({"name": "Daily task", "preset_id": "ask",
                                         "time": "08:00", "days": [0]}, account["id"])
        now = self.e.datetime.datetime(2025, 1, 6, 8, 1)
        with patch.object(self.e, "list_accounts", return_value=[account]), patch.object(
            self.e, "enqueue_task", side_effect=ValueError("preset missing")
        ):
            self.e.tick(now)
        saved = self.e.get_schedules(account["id"])
        self.assertEqual(saved[0]["id"], schedule["id"])
        self.assertEqual(saved[0]["last_run"], "")

    def test_successful_schedule_enqueue_records_its_run_day(self):
        account = self.e.create_account("Schedule success", "schedule-success-password")
        self.e.save_briefing_settings({"enabled": False}, account["id"])
        schedule = self.e.save_schedule({"name": "Daily task", "preset_id": "ask",
                                         "time": "08:00", "days": [0]}, account["id"])
        now = self.e.datetime.datetime(2025, 1, 6, 8, 1)
        with patch.object(self.e, "list_accounts", return_value=[account]), patch.object(
            self.e, "enqueue_task", return_value=123
        ) as enqueue:
            self.e.tick(now)
        enqueue.assert_called_once()
        saved = self.e.get_schedules(account["id"])
        self.assertEqual(saved[0]["id"], schedule["id"])
        self.assertEqual(saved[0]["last_run"], now.date().isoformat())

    def test_schedule_requires_selected_days_and_valid_time(self):
        account = self.e.create_account("Schedule validation", "validation-password")
        for data in ({"days": [], "time": "08:00"}, {"days": [0], "time": "25:61"}):
            with self.assertRaises(ValueError):
                self.e.save_schedule({"preset_id": "ask", **data}, account["id"])
        with self.assertRaisesRegex(ValueError, "links must be a list"):
            self.e.save_schedule({"preset_id": "ask", "days": [0], "time": "08:00",
                                  "urls": "https://example.com"}, account["id"])

    def test_failed_automatic_briefing_enqueue_can_retry(self):
        account = self.e.create_account("Briefing retry", "briefing-retry-password")
        self.e.save_briefing_settings({"enabled": True, "deliver_time": "08:00",
                                       "prep_minutes": 45}, account["id"])
        now = self.e.datetime.datetime(2025, 1, 6, 7, 30)
        attempt_key = self.e.account_key(account["id"], "last_build_attempt")
        with patch.object(self.e, "list_accounts", return_value=[account]), patch.object(
            self.e, "enqueue_briefing", side_effect=RuntimeError("database unavailable")
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                self.e.tick(now)
        self.assertIsNone(self.e.kv_get(attempt_key))

    def test_system_status_uses_cached_health_instead_of_network_calls(self):
        with patch.object(self.e, "ollama_up", side_effect=AssertionError("network call")), patch.object(
            self.e, "pc_host_up", side_effect=AssertionError("network call")
        ), patch.object(self.e, "pc_has_active_sessions", side_effect=AssertionError("network call")):
            status = self.e.system_status()
        self.assertFalse(status["health_checked"])
        self.assertIsNone(status["pc_up"])
        self.assertIsNone(status["active_sessions"])
        self.assertFalse(status["session_check_ok"])

    def test_health_refresh_caches_unknown_session_check_without_claiming_no_session(self):
        original_health = dict(self.e._health)
        try:
            with patch.object(self.e, "ollama_up", return_value=True), patch.object(
                self.e, "pc_session_status", return_value=None
            ), patch.object(self.e.time, "time", return_value=123):
                self.e.refresh_health()
            status = self.e.system_status()
            self.assertTrue(status["pc_up"])
            self.assertTrue(status["pc_on"])
            self.assertTrue(status["health_checked"])
            self.assertFalse(status["session_check_ok"])
            self.assertFalse(status["active_sessions"])
        finally:
            self.e._health.update(original_health)

    def test_auto_poweroff_serializes_against_new_pc_work(self):
        original_idle = self.e.IDLE_OFF_MIN
        original_state = dict(self.e.state)
        self.e.IDLE_OFF_MIN = 1
        self.e.state.update(last=0, busy=0, worker_busy=False, we_woke=True)
        off_started, allow_off, job_started = threading.Event(), threading.Event(), threading.Event()

        def delayed_poweroff():
            off_started.set()
            allow_off.wait(2)
            return True

        with patch.object(self.e, "power_off_pc_if_idle", side_effect=delayed_poweroff), patch.object(
            self.e, "ensure_pc"
        ):
            off_thread = threading.Thread(target=self.e.power_off_if_idle)
            off_thread.start()
            self.assertTrue(off_started.wait(1))

            def start_job():
                with self.e.pc_job():
                    job_started.set()

            job_thread = threading.Thread(target=start_job)
            job_thread.start()
            self.assertFalse(job_started.wait(0.05))
            allow_off.set()
            off_thread.join(2)
            job_thread.join(2)

        self.e.IDLE_OFF_MIN = original_idle
        self.e.state.update(original_state)
        self.assertFalse(off_thread.is_alive())
        self.assertFalse(job_thread.is_alive())
        self.assertTrue(job_started.is_set())


if __name__ == "__main__":
    unittest.main()
