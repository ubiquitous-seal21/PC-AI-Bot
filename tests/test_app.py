import importlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


class AppRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["HUB_PASSWORD"] = "test-hub-password"
        os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="pcaibot-app-test-")
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ai", "hub")))
        cls.hub = importlib.import_module("app")
        cls.hub.app.config["TESTING"] = True

    def setUp(self):
        self.client = self.hub.app.test_client()
        response = self.client.post("/api/login", json={
            "username": "admin", "password": "test-hub-password"
        })
        self.assertEqual(response.status_code, 200, response.json)
        self.account_id = response.json["account"]["id"]

    def test_chat_route_forwards_history_and_rejects_bad_payloads(self):
        history = [{"role": "user", "content": "Earlier question"},
                   {"role": "assistant", "content": "Earlier answer"}]
        with patch.object(self.hub.E, "enqueue_chat", return_value=123) as enqueue:
            response = self.client.post("/api/chat", headers={"X-Hub": "1"},
                                        json={"message": "Follow-up", "history": history})
        self.assertEqual(response.status_code, 200, response.json)
        enqueue.assert_called_once_with("Follow-up", 1, history)

        invalid = self.client.post("/api/chat", headers={"X-Hub": "1"}, json=[])
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json["error"], "Expected a JSON object.")

    def test_pause_route_requires_a_boolean(self):
        self.hub.E.set_paused(False)
        response = self.client.put("/api/pc/pause", headers={"X-Hub": "1"},
                                   json={"paused": "false"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.hub.E.state["paused"])

    def test_stale_account_session_is_cleared(self):
        client = self.hub.app.test_client()
        with client.session_transaction() as session:
            session["auth"] = True
            session["account_id"] = 99999
        response = client.get("/api/state")
        self.assertEqual(response.status_code, 401)
        with client.session_transaction() as session:
            self.assertNotIn("auth", session)

    def test_briefing_settings_reports_malformed_focuses(self):
        response = self.client.put("/api/briefing/settings", headers={"X-Hub": "1"},
                                   json={"focuses": ["bad"]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Focus areas", response.json["error"])

    def test_state_only_shows_current_accounts_errors(self):
        other = self.hub.E.create_account("Other account", "other-password")
        other_run = self.hub.E.new_run("task", "Private failure", account_id=other["id"])
        self.hub.E.update_run(other_run, status="error", detail="private")
        current = self.client.get("/api/state")
        self.assertEqual(current.status_code, 200)
        self.assertNotIn("Private failure", [r["title"] for r in current.json["recent_errors"]])

        own_run = self.hub.E.new_run("task", "Own failure", account_id=self.account_id)
        self.hub.E.update_run(own_run, status="error", detail="expected")
        current = self.client.get("/api/state")
        self.assertIn("Own failure", [r["title"] for r in current.json["recent_errors"]])


if __name__ == "__main__":
    unittest.main()
