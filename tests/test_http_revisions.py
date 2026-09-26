import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class HttpRevisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        static_dir = str(Path(__file__).resolve().parent.parent / "static")
        self.server = create_server("127.0.0.1", 0, self.service, RuleEngine(), static_dir)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _url(self, path):
        return "http://127.0.0.1:%s%s" % (self.port, path)

    def _request(self, method, path, body=None, user="admin", role="admin"):
        data = None
        headers = {"X-User-Id": user, "X-Role": role}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self._url(path), data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _create_published_event(self):
        status, body = self._request("POST", "/api/events", {
            "title": "HTTP-Event",
            "origin_time": "2026-09-26T00:00:00Z",
            "location": "Region",
            "reports": [
                {"station": "A", "time_offset": 1, "distance_km": 1.0},
                {"station": "B", "time_offset": -1, "distance_km": 1.2},
            ],
        })
        self.assertEqual(status, 201)
        event_id = body["id"]
        for action, data, version in [
            ("associate", {}, 1),
            ("review", {"reviewer": "R-1", "magnitude": 4.2}, 2),
            ("publish", {"communication_id": "C-9"}, 3),
        ]:
            status, body = self._request(
                "POST",
                "/api/entities/%s/actions" % event_id,
                {"action": action, "data": data, "expected_version": version},
            )
            self.assertEqual(status, 200, body)
        return event_id

    def test_revisions_endpoint_returns_full_timeline(self):
        event_id = self._create_published_event()
        status, body = self._request("GET", "/api/events/%s/revisions" % event_id)
        self.assertEqual(status, 200)
        actions = [item["action"] for item in body["items"]]
        self.assertEqual(actions, ["create", "associate", "review", "publish"])
        review = body["items"][2]
        self.assertEqual(review["from_magnitude"], None)
        self.assertEqual(review["to_magnitude"], "4.2")
        self.assertEqual(review["actor_id"], "admin")

        # entities alias works too
        status, alias = self._request("GET", "/api/entities/%s/revisions" % event_id)
        self.assertEqual(status, 200)
        self.assertEqual(len(alias["items"]), 4)

    def test_stale_submission_returns_409_with_current_version(self):
        event_id = self._create_published_event()
        status, body = self._request(
            "POST",
            "/api/entities/%s/actions" % event_id,
            {
                "action": "revise",
                "data": {"reason": "late", "magnitude": 5.1},
                "expected_version": 2,
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["type"], "ConflictError")
        self.assertEqual(body["expected_version"], 2)
        self.assertEqual(body["current_version"], 4)
        self.assertIn("stale", body["error"])

        status, current = self._request("GET", "/api/entities/%s" % event_id)
        self.assertEqual(current["version"], 4)
        self.assertEqual(current["status"], "published")

    def test_first_view_of_legacy_event_recorded_over_http(self):
        # Seed a legacy event directly.
        self.repo.create_entity(
            "legacy-http",
            "event",
            "published",
            {"title": "Old", "magnitude": 3.1},
            "legacy",
        )
        status, body = self._request(
            "GET", "/api/entities/legacy-http", user="rev-9", role="reviewer"
        )
        self.assertEqual(status, 200)
        status, body = self._request(
            "GET", "/api/events/legacy-http/revisions", user="rev-9", role="reviewer"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["action"], "first_view")
        self.assertEqual(body["items"][0]["actor_id"], "rev-9")
        self.assertEqual(body["items"][0]["to_magnitude"], "3.1")

    def test_revisions_endpoint_404_for_missing_event(self):
        status, body = self._request("GET", "/api/events/no-such/revisions")
        self.assertEqual(status, 404)

    def test_index_page_still_served(self):
        request = urllib.request.Request(self._url("/"))
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("修订记录", response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
