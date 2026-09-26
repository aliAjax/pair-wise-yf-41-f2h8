import json
import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def make_event(service, actor=None):
    actor = actor or Actor("analyst-1", "analyst")
    return service.create(
        actor,
        "event",
        {
            "title": "Event-A",
            "origin_time": "2026-09-26T00:00:00Z",
            "location": "Region-A",
            "reports": [
                {"station": "STA-1", "time_offset": 2, "distance_km": 1.0},
                {"station": "STA-2", "time_offset": -1, "distance_km": 1.5},
            ],
        },
    )


def drive_to_published(service, event_id):
    admin = Actor("admin", "admin")
    service.transition(admin, event_id, "associate", {}, 1)
    service.transition(admin, event_id, "review", {"reviewer": "R-1", "magnitude": 4.2}, 2)
    service.transition(
        admin, event_id, "publish", {"communication_id": "C-1"}, 3
    )


class EventRevisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_lifecycle_records_ordered_revisions_with_snapshots(self):
        event = make_event(self.service, self.admin)
        drive_to_published(self.service, event["id"])
        revised = self.service.transition(
            self.admin,
            event["id"],
            "revise",
            {"reason": "new station data", "magnitude": 4.5},
            4,
        )
        withdrawn = self.service.transition(
            self.admin, event["id"], "withdraw", {"reason": "false trigger"}, 5
        )

        revisions = self.service.revisions(event["id"])
        self.assertEqual(
            [r["action"] for r in revisions],
            ["create", "associate", "review", "publish", "revise", "withdraw"],
        )
        self.assertEqual([r["seq"] for r in revisions], [1, 2, 3, 4, 5, 6])

        review = revisions[2]
        self.assertEqual(review["from_status"], "associated")
        self.assertEqual(review["to_status"], "reviewed")
        self.assertEqual(review["from_version"], 2)
        self.assertEqual(review["to_version"], 3)
        self.assertIsNone(review["from_magnitude"])
        self.assertEqual(review["to_magnitude"], "4.2")
        self.assertEqual(review["actor_id"], "admin")
        self.assertEqual(review["actor_role"], "admin")

        revise = revisions[4]
        self.assertEqual(revise["from_magnitude"], "4.2")
        self.assertEqual(revise["to_magnitude"], "4.5")
        self.assertEqual(revise["reason"], "new station data")
        self.assertEqual(revise["to_status"], "revised")

        withdraw = revisions[5]
        self.assertEqual(withdraw["from_status"], "revised")
        self.assertEqual(withdraw["to_status"], "withdrawn")
        self.assertEqual(withdraw["reason"], "false trigger")
        self.assertEqual(withdraw["to_magnitude"], "4.5")

        self.assertEqual(revised["version"], 5)
        self.assertEqual(withdrawn["status"], "withdrawn")

    def test_publish_records_revision_with_communication_reference(self):
        event = make_event(self.service, self.admin)
        drive_to_published(self.service, event["id"])
        publish = self.service.revisions(event["id"])[3]
        self.assertEqual(publish["action"], "publish")
        self.assertEqual(publish["to_status"], "published")
        self.assertEqual(publish["reason"], "C-1")

    def test_stale_version_is_rejected_without_touching_record_or_history(self):
        event = make_event(self.service, self.admin)
        drive_to_published(self.service, event["id"])

        fresh = self.service.get(event["id"])
        self.assertEqual(fresh["version"], 4)

        with self.assertRaises(ConflictError) as caught:
            self.service.transition(
                self.admin,
                event["id"],
                "revise",
                {"reason": "late edit", "magnitude": 5.0},
                expected_version=3,
            )
        error = caught.exception
        self.assertEqual(error.expected_version, 3)
        self.assertEqual(error.current_version, 4)
        self.assertIn("stale", str(error))

        untouched = self.service.get(event["id"])
        self.assertEqual(untouched["version"], 4)
        self.assertEqual(untouched["status"], "published")
        self.assertEqual(untouched["data"]["magnitude"], 4.2)

        actions = [r["action"] for r in self.service.revisions(event["id"])]
        self.assertEqual(actions, ["create", "associate", "review", "publish"])

    def test_stale_version_after_concurrent_update(self):
        event = make_event(self.service, self.admin)
        drive_to_published(self.service, event["id"])
        # Another reviewer revises first, bumping the version to 5.
        self.service.transition(
            Actor("reviewer-a", "reviewer"),
            event["id"],
            "revise",
            {"reason": "first revision", "magnitude": 4.4},
            4,
        )
        with self.assertRaises(ConflictError) as caught:
            self.service.transition(
                Actor("reviewer-b", "reviewer"),
                event["id"],
                "revise",
                {"reason": "second revision", "magnitude": 4.6},
                4,
            )
        self.assertEqual(caught.exception.current_version, 5)

    def test_legacy_event_first_view_adds_baseline_once(self):
        # Simulate an event created before the revision feature existed:
        # insert directly through the repository without any revision rows.
        legacy = self.repo.create_entity(
            "legacy-1",
            "event",
            "published",
            {
                "title": "Old event",
                "origin_time": "2025-01-01T00:00:00Z",
                "location": "Old region",
                "magnitude": 3.8,
            },
            "legacy-system",
        )
        self.assertEqual(self.service.revisions("legacy-1"), [])

        reviewer = Actor("reviewer-2", "reviewer")
        viewed = self.service.get("legacy-1", actor=reviewer)
        self.assertEqual(viewed["id"], "legacy-1")

        revisions = self.service.revisions("legacy-1")
        self.assertEqual(len(revisions), 1)
        baseline = revisions[0]
        self.assertEqual(baseline["action"], "first_view")
        self.assertEqual(baseline["to_status"], "published")
        self.assertEqual(baseline["to_version"], 1)
        self.assertEqual(baseline["to_magnitude"], "3.8")
        self.assertIsNone(baseline["from_version"])
        self.assertEqual(baseline["actor_id"], "reviewer-2")
        self.assertTrue(baseline["detail"]["baseline"])

        # A second view (even by another user) must not add another baseline.
        self.service.get("legacy-1", actor=Actor("reviewer-3", "reviewer"))
        self.service.revisions("legacy-1", actor=Actor("reviewer-4", "reviewer"))
        self.assertEqual(len(self.service.revisions("legacy-1")), 1)

    def test_new_event_has_no_first_view_baseline(self):
        event = make_event(self.service, self.admin)
        self.service.get(event["id"], actor=Actor("viewer-1", "viewer"))
        actions = [r["action"] for r in self.service.revisions(event["id"])]
        self.assertEqual(actions, ["create"])

    def test_revisions_rejected_for_non_events(self):
        station = self.service.create(
            self.admin, "station", {"code": "STA-9", "lat": 1.0, "lon": 2.0}
        )
        with self.assertRaises(ValidationError):
            self.service.revisions(station["id"])

    def test_revision_table_is_append_only(self):
        event = make_event(self.service, self.admin)
        drive_to_published(self.service, event["id"])
        with self.repo._connect() as connection:
            with self.assertRaises(Exception):
                connection.execute(
                    "UPDATE event_revisions SET to_magnitude = '9.9' WHERE seq = 1"
                )
            with self.assertRaises(Exception):
                connection.execute("DELETE FROM event_revisions WHERE seq = 1")
        # Snapshot must survive the blocked writes.
        revisions = self.service.revisions(event["id"])
        self.assertEqual(len(revisions), 4)
        self.assertIsNone(revisions[0]["to_magnitude"])


if __name__ == "__main__":
    unittest.main()
