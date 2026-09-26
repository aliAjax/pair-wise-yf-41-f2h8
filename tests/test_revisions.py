import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, NotFoundError, StaleVersionError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def event_data():
    return {
        "title": "Event-A",
        "origin_time": "2026-01-01T00:00:00Z",
        "location": "Region-A",
        "reports": [
            {"station": "STA-1", "time_offset": 2, "distance_km": 1.0},
            {"station": "STA-2", "time_offset": -1, "distance_km": 1.5},
        ],
    }


class EventRevisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.analyst = Actor("analyst-1", "analyst")
        self.reviewer = Actor("reviewer-1", "reviewer")
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _publish_event(self):
        event = self.service.create(self.analyst, "event", event_data())
        event = self.service.transition(self.admin, event["id"], "associate", {})
        event = self.service.transition(
            self.reviewer, event["id"], "review",
            {"reviewer": "reviewer-1", "magnitude": 4.2},
        )
        event = self.service.transition(
            self.reviewer, event["id"], "publish",
            {"communication_id": "C-1"},
        )
        return event

    def test_full_lifecycle_leaves_ordered_revision_trail(self):
        event = self._publish_event()
        revised = self.service.transition(
            self.reviewer, event["id"], "revise",
            {"reason": "new station data", "magnitude": 4.3},
        )
        self.assertEqual(revised["status"], "revised")

        revisions = self.service.event_revisions(event["id"])
        actions = [r["action"] for r in revisions]
        self.assertEqual(
            actions, ["create", "associate", "review", "publish", "revise"]
        )
        self.assertEqual([r["seq"] for r in revisions], [1, 2, 3, 4, 5])

        create = revisions[0]
        self.assertIsNone(create["from_version"])
        self.assertEqual(create["to_version"], 1)
        self.assertEqual(create["to_status"], "candidate")
        self.assertIsNone(create["to_magnitude"])
        self.assertEqual(create["actor_id"], "analyst-1")
        self.assertIsNotNone(create["created_at"])

        review = revisions[2]
        self.assertEqual((review["from_version"], review["to_version"]), (2, 3))
        self.assertEqual(review["from_status"], "associated")
        self.assertEqual(review["to_status"], "reviewed")
        self.assertIsNone(review["from_magnitude"])
        self.assertEqual(review["to_magnitude"], 4.2)

        revise = revisions[-1]
        self.assertEqual((revise["from_magnitude"], revise["to_magnitude"]), (4.2, 4.3))
        self.assertEqual((revise["from_status"], revise["to_status"]), ("published", "revised"))
        self.assertEqual(revise["reason"], "new station data")
        self.assertEqual(revise["actor_id"], "reviewer-1")
        # snapshot lets reviewers return to the record as it existed at that revision
        self.assertEqual(revise["snapshot"]["magnitude"], 4.3)
        self.assertEqual(revise["snapshot"]["reason"], "new station data")
        prior = revisions[-2]
        self.assertEqual(prior["snapshot"]["communication_id"], "C-1")

    def test_get_single_revision_returns_snapshot(self):
        event = self._publish_event()
        revision = self.service.event_revision(event["id"], 3)
        self.assertEqual(revision["action"], "review")
        self.assertEqual(revision["snapshot"]["magnitude"], 4.2)
        with self.assertRaises(NotFoundError):
            self.service.event_revision(event["id"], 99)

    def test_stale_expected_version_is_rejected_and_record_untouched(self):
        event = self._publish_event()
        with self.assertRaises(StaleVersionError) as caught:
            self.service.transition(
                self.reviewer, event["id"], "revise",
                {"reason": "stale change", "magnitude": 5.0},
                expected_version=1,
            )
        self.assertEqual(caught.exception.expected_version, 1)
        self.assertEqual(caught.exception.current_version, 4)
        self.assertTrue("out of date" in str(caught.exception))
        # still a ConflictError for callers that catch the base type
        self.assertIsInstance(caught.exception, ConflictError)

        unchanged = self.service.get(event["id"])
        self.assertEqual(unchanged["version"], 4)
        self.assertEqual(unchanged["status"], "published")
        self.assertEqual(unchanged["data"].get("magnitude"), 4.2)
        self.assertNotIn("stale change", unchanged["data"].get("reason", ""))

        # no revision row was written by the failed submission
        actions = [r["action"] for r in self.service.event_revisions(event["id"])]
        self.assertEqual(actions, ["create", "associate", "review", "publish"])

    def test_withdraw_is_recorded_with_reason_and_status_change(self):
        event = self._publish_event()
        self.service.transition(
            self.reviewer, event["id"], "withdraw", {"reason": "false alarm"},
        )
        revision = self.service.event_revisions(event["id"])[-1]
        self.assertEqual(revision["action"], "withdraw")
        self.assertEqual(revision["to_status"], "withdrawn")
        self.assertEqual(revision["reason"], "false alarm")
        self.assertEqual(revision["to_magnitude"], 4.2)

    def test_legacy_event_gets_baseline_on_first_view_only_once(self):
        # Simulate an event created before revision tracking existed.
        event = self.service.create(self.analyst, "event", event_data())
        with self.repo._connect() as connection:
            connection.execute(
                "DELETE FROM event_revisions WHERE entity_id = ?", (event["id"],)
            )
        self.assertEqual(self.repo.list_event_revisions(event["id"]), [])

        viewer = Actor("viewer-7", "viewer")
        seen = self.service.get(event["id"], actor=viewer)
        revisions = self.service.event_revisions(event["id"])
        self.assertEqual(len(revisions), 1)
        baseline = revisions[0]
        self.assertEqual(baseline["action"], "first_view")
        self.assertEqual(baseline["seq"], 1)
        self.assertEqual(baseline["to_version"], seen["version"])
        self.assertEqual(baseline["to_status"], seen["status"])
        self.assertEqual(baseline["actor_id"], "viewer-7")
        self.assertTrue("首次查看" in baseline["reason"])
        self.assertEqual(baseline["snapshot"]["title"], "Event-A")

        # A second view must not add another baseline.
        self.service.get(event["id"], actor=self.reviewer)
        self.assertEqual(len(self.service.event_revisions(event["id"])), 1)

    def test_revisions_not_available_for_stations(self):
        station = self.service.create(
            self.admin, "station", {"code": "STA-1", "lat": 35.0, "lon": 110.0}
        )
        with self.assertRaises(Exception):
            self.service.event_revisions(station["id"])


if __name__ == "__main__":
    unittest.main()
