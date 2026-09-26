import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        import os
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

    def url(self, path):
        return "http://127.0.0.1:%s%s" % (self.port, path)

    def call(self, method, path, body=None, user="reviewer-1", role="reviewer"):
        data = None
        headers = {"X-User-Id": user, "X-Role": role}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urlrequest.Request(self.url(path), data=data, headers=headers, method=method)
        try:
            with urlrequest.urlopen(req) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _create_published_event(self):
        payload = {
            "title": "HTTP-Event",
            "origin_time": "2026-01-01T00:00:00Z",
            "location": "Region-A",
            "reports": [
                {"station": "STA-1", "time_offset": 2, "distance_km": 1.0},
                {"station": "STA-2", "time_offset": -1, "distance_km": 1.5},
            ],
        }
        _, event = self.call("POST", "/api/events", payload, user="analyst-1", role="analyst")
        self.call("POST", "/api/entities/%s/actions" % event["id"],
                  {"action": "associate", "data": {}}, user="admin", role="admin")
        self.call("POST", "/api/entities/%s/actions" % event["id"],
                  {"action": "review", "data": {"reviewer": "R-1", "magnitude": 4.2}})
        self.call("POST", "/api/entities/%s/actions" % event["id"],
                  {"action": "publish", "data": {"communication_id": "C-9"}})
        return event["id"]

    def test_revisions_endpoint_returns_ordered_trail(self):
        event_id = self._create_published_event()
        status, body = self.call("GET", "/api/events/%s/revisions" % event_id)
        self.assertEqual(status, 200)
        self.assertEqual([r["action"] for r in body["items"]],
                         ["create", "associate", "review", "publish"])
        self.assertEqual(body["items"][-1]["to_status"], "published")

        status, body = self.call("GET", "/api/events/%s/revisions/3" % event_id)
        self.assertEqual(status, 200)
        self.assertEqual(body["seq"], 3)
        self.assertEqual(body["snapshot"]["magnitude"], 4.2)

        # /api/entities/... aliases also expose the trail
        status, body = self.call("GET", "/api/entities/%s/revisions" % event_id)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 4)

    def test_revise_and_stale_version_response(self):
        event_id = self._create_published_event()
        status, body = self.call(
            "POST", "/api/entities/%s/actions" % event_id,
            {"action": "revise", "data": {"reason": "extra data", "magnitude": 4.4},
             "expected_version": 4},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 5)

        status, body = self.call(
            "POST", "/api/entities/%s/actions" % event_id,
            {"action": "revise", "data": {"reason": "stale", "magnitude": 5.1},
             "expected_version": 4},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["type"], "StaleVersionError")
        self.assertEqual(body["expected_version"], 4)
        self.assertEqual(body["current_version"], 5)
        self.assertIn("out of date", body["error"])

        # entity itself stayed at version 5 / magnitude 4.4
        status, event = self.call("GET", "/api/entities/%s" % event_id)
        self.assertEqual(event["version"], 5)
        self.assertEqual(event["data"]["magnitude"], 4.4)

        _, trail = self.call("GET", "/api/events/%s/revisions" % event_id)
        self.assertEqual([r["action"] for r in trail["items"]][-1], "revise")

    def test_legacy_event_first_view_records_baseline(self):
        # create event then remove its trail to emulate a pre-tracking event
        _, event = self.call(
            "POST", "/api/events",
            {"title": "Legacy", "origin_time": "2026-01-01T00:00:00Z",
             "location": "X", "reports": [
                 {"station": "A", "time_offset": 1, "distance_km": 0.5},
                 {"station": "B", "time_offset": 2, "distance_km": 1.0}]},
            user="analyst-1", role="analyst",
        )
        with self.repo._connect() as connection:
            connection.execute(
                "DELETE FROM event_revisions WHERE entity_id = ?", (event["id"],)
            )

        status, body = self.call(
            "GET", "/api/events/%s/revisions" % event["id"], user="viewer-2", role="viewer"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["action"], "first_view")
        self.assertEqual(body["items"][0]["actor_id"], "viewer-2")

        # second view adds nothing
        status, body = self.call("GET", "/api/events/%s/revisions" % event["id"])
        self.assertEqual(len(body["items"]), 1)

    def test_index_page_served(self):
        req = urlrequest.Request(self.url("/"))
        with urlrequest.urlopen(req) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("修订记录", response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
