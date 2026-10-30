import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kudos.http_server import create_server
from kudos.service import (
    AuthenticationError,
    AuthorizationError,
    KudosService,
    NotFoundError,
    RateLimitError,
    ValidationError,
)


class KudosServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = KudosService(str(Path(self.temp_dir.name) / "kudos.db"))
        self.service.seed_demo_employees()

    def tearDown(self):
        self.temp_dir.cleanup()

    def send(self, message="Thanks for your help!", recipient="E003"):
        return self.service.create_kudos("E002", recipient, message)

    def test_seeded_employee_picker_excludes_self(self):
        employees = self.service.list_employees("E002")
        self.assertEqual([employee["employee_id"] for employee in employees], ["E001", "E003"])

    def test_employee_search_is_case_insensitive(self):
        employees = self.service.list_employees("E002", "TAYLOR")
        self.assertEqual([employee["display_name"] for employee in employees], ["Taylor Employee"])

    def test_inactive_or_unknown_actor_cannot_use_service(self):
        with self.assertRaises(AuthenticationError):
            self.service.list_employees("missing")
        with self.service.connect() as connection:
            connection.execute("UPDATE employees SET active=0 WHERE employee_id='E002'")
        with self.assertRaises(AuthenticationError):
            self.service.list_employees("E002")

    def test_create_kudos_trims_and_records_server_identity(self):
        item = self.send("  Great work on the release!  ")
        self.assertEqual(item["message"], "Great work on the release!")
        self.assertEqual(item["sender_id"], "E002")
        self.assertEqual(item["recipient_name"], "Taylor Employee")
        self.assertTrue(item["is_visible"])

    def test_create_kudos_rejects_self_award(self):
        with self.assertRaises(ValidationError):
            self.service.create_kudos("E002", "E002", "Thanks!")

    def test_create_kudos_rejects_invalid_messages_and_recipient(self):
        for message in ("", " \n ", "x" * 501, None):
            with self.subTest(message=repr(message)), self.assertRaises(ValidationError):
                self.send(message=message)
        with self.assertRaises(ValidationError):
            self.send(recipient=None)

    def test_create_kudos_rejects_inactive_recipient(self):
        with self.service.connect() as connection:
            connection.execute("UPDATE employees SET active=0 WHERE employee_id='E003'")
        with self.assertRaises(ValidationError):
            self.send()

    def test_feed_only_shows_visible_non_deleted_kudos(self):
        item = self.send()
        self.service.moderate("E001", item["kudos_id"], "hide", "Review")
        self.assertEqual(self.service.feed("E002")["items"], [])
        self.service.moderate("E001", item["kudos_id"], "restore")
        self.assertEqual(len(self.service.feed("E002")["items"]), 1)

    def test_feed_pages_in_stable_order(self):
        first = self.send("first")
        second = self.send("second")
        page_one = self.service.feed("E002", limit=1)
        self.assertEqual(len(page_one["items"]), 1)
        self.assertTrue(page_one["next_cursor"])
        page_two = self.service.feed(
            "E002", limit=1, cursor=page_one["next_cursor"]
        )
        all_ids = {first["kudos_id"], second["kudos_id"]}
        self.assertEqual(
            {page_one["items"][0]["kudos_id"], page_two["items"][0]["kudos_id"]},
            all_ids,
        )
        self.assertIsNone(page_two["next_cursor"])

    def test_feed_rejects_invalid_limit_and_cursor(self):
        for limit in (0, 101):
            with self.subTest(limit=limit), self.assertRaises(ValidationError):
                self.service.feed("E002", limit=limit)
        with self.assertRaises(ValidationError):
            self.service.feed("E002", cursor="invalid")

    def test_identical_repeat_is_rejected_within_window(self):
        self.send("Great job!")
        with self.assertRaises(RateLimitError):
            self.send("  Great job!  ")

    def test_submission_rate_limit_is_enforced(self):
        for index in range(10):
            self.send(f"Thank you number {index}")
        with self.assertRaises(RateLimitError):
            self.send("An eleventh message")

    def test_submission_limits_are_configurable(self):
        limited = KudosService(
            str(Path(self.temp_dir.name) / "limited.db"),
            submissions_per_hour=1,
            duplicate_window_seconds=5,
        )
        limited.seed_demo_employees()
        limited.create_kudos("E002", "E003", "First")
        with self.assertRaises(RateLimitError):
            limited.create_kudos("E002", "E001", "Second")

    def test_non_moderator_cannot_moderate(self):
        item = self.send()
        with self.assertRaises(AuthorizationError):
            self.service.moderate("E002", item["kudos_id"], "hide")

    def test_moderation_queue_only_contains_hidden_kudos(self):
        visible = self.send("Visible kudos")
        hidden = self.send("Different hidden kudos")
        self.service.moderate("E001", hidden["kudos_id"], "hide", "Review")
        queue = self.service.moderation_queue("E001")
        self.assertEqual([entry["kudos_id"] for entry in queue], [hidden["kudos_id"]])
        with self.assertRaises(AuthorizationError):
            self.service.moderation_queue("E002")

    def test_restore_records_moderation_audit(self):
        item = self.send()
        self.service.moderate("E001", item["kudos_id"], "hide")
        self.service.moderate("E001", item["kudos_id"], "restore")
        with self.service.connect() as connection:
            actions = [
                row["action"]
                for row in connection.execute(
                    "SELECT action FROM moderation_audit ORDER BY occurred_at, audit_id"
                )
            ]
        self.assertCountEqual(actions, ["hide", "restore"])

    def test_delete_removes_message_and_participant_references(self):
        item = self.send("Sensitive message")
        self.service.moderate("E001", item["kudos_id"], "delete", "Policy violation")
        self.assertEqual(self.service.feed("E002")["items"], [])
        with self.service.connect() as connection:
            row = connection.execute(
                "SELECT sender_id, recipient_id, message, deleted_at FROM kudos WHERE kudos_id=?",
                (item["kudos_id"],),
            ).fetchone()
            audit = connection.execute(
                "SELECT action, reason FROM moderation_audit WHERE kudos_id=?",
                (item["kudos_id"],),
            ).fetchone()
        self.assertIsNone(row["sender_id"])
        self.assertIsNone(row["recipient_id"])
        self.assertEqual(row["message"], "")
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual((audit["action"], audit["reason"]), ("delete", "Policy violation"))

    def test_moderation_rejects_invalid_action_reason_and_unknown_id(self):
        item = self.send()
        with self.assertRaises(ValidationError):
            self.service.moderate("E001", item["kudos_id"], "remove")
        with self.assertRaises(ValidationError):
            self.service.moderate("E001", item["kudos_id"], "hide", "r" * 501)
        with self.assertRaises(NotFoundError):
            self.service.moderate("E001", "missing-id", "hide")


class KudosHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        service = KudosService(str(Path(self.temp_dir.name) / "api.db"))
        service.seed_demo_employees()
        self.server = create_server(
            service,
            {"employee-token": "E002", "moderator-token": "E001"},
            port=0,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def request(self, path, token=None, method="GET", body=None):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_api_requires_bearer_authentication(self):
        status, payload = self.request("/api/kudos")
        self.assertEqual(status, 401)
        self.assertIn("error", payload)

    def test_root_serves_the_kudos_ui(self):
        with urlopen(self.base_url + "/") as response:
            html = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn("<title>Datacom Kudos</title>", html)

    def test_api_creates_kudos_and_returns_feed(self):
        status, created = self.request(
            "/api/kudos",
            "employee-token",
            "POST",
            {"recipient_id": "E003", "message": "Thanks!"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["sender_id"], "E002")
        status, feed = self.request("/api/kudos", "employee-token")
        self.assertEqual(status, 200)
        self.assertEqual(feed["items"][0]["kudos_id"], created["kudos_id"])

    def test_api_moderator_can_hide_and_restore(self):
        _, created = self.request(
            "/api/kudos",
            "employee-token",
            "POST",
            {"recipient_id": "E003", "message": "Thanks!"},
        )
        status, _ = self.request(
            f"/api/kudos/{created['kudos_id']}/hide",
            "moderator-token",
            "POST",
            {"reason": "Review"},
        )
        self.assertEqual(status, 200)
        status, queue = self.request("/api/moderation/kudos", "moderator-token")
        self.assertEqual(status, 200)
        self.assertEqual(len(queue["items"]), 1)
        status, _ = self.request(
            f"/api/kudos/{created['kudos_id']}/restore",
            "moderator-token",
            "POST",
        )
        self.assertEqual(status, 200)

    def test_api_denies_employee_moderation(self):
        _, created = self.request(
            "/api/kudos",
            "employee-token",
            "POST",
            {"recipient_id": "E003", "message": "Thanks!"},
        )
        status, _ = self.request(
            f"/api/kudos/{created['kudos_id']}/hide",
            "employee-token",
            "POST",
        )
        self.assertEqual(status, 403)

    def test_api_moderator_can_delete_kudos(self):
        _, created = self.request(
            "/api/kudos",
            "employee-token",
            "POST",
            {"recipient_id": "E003", "message": "Thanks!"},
        )
        status, result = self.request(
            f"/api/kudos/{created['kudos_id']}",
            "moderator-token",
            "DELETE",
            {"reason": "Policy violation"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["action"], "delete")
        status, feed = self.request("/api/kudos", "employee-token")
        self.assertEqual(status, 200)
        self.assertEqual(feed["items"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
