import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS event_revisions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_version INTEGER,
                    to_version INTEGER NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    from_magnitude TEXT,
                    to_magnitude TEXT,
                    reason TEXT,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_event_revisions_entity
                    ON event_revisions(entity_id, seq);
                CREATE TRIGGER IF NOT EXISTS trg_event_revisions_no_update
                    BEFORE UPDATE ON event_revisions
                BEGIN
                    SELECT RAISE(ABORT, 'event_revisions is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_event_revisions_no_delete
                    BEFORE DELETE ON event_revisions
                BEGIN
                    SELECT RAISE(ABORT, 'event_revisions is append-only');
                END;
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id, revision=None):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
            if revision:
                self._insert_revision(connection, revision, now=now)
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data, revision=None):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "stale version: expected_version %s is out of date, "
                    "current version is %s; refetch and resubmit"
                    % (expected_version, current_version),
                    expected_version=int(expected_version),
                    current_version=current_version,
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            if revision:
                self._insert_revision(connection, revision, now=now)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _insert_revision(connection, revision, now=None):
        detail = revision.get("detail") or {}
        connection.execute(
            "INSERT INTO event_revisions(entity_id, action, from_version, to_version, "
            "from_status, to_status, from_magnitude, to_magnitude, reason, "
            "actor_id, actor_role, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision["entity_id"],
                revision["action"],
                revision.get("from_version"),
                revision["to_version"],
                revision.get("from_status"),
                revision["to_status"],
                revision.get("from_magnitude"),
                revision.get("to_magnitude"),
                revision.get("reason"),
                revision["actor_id"],
                revision["actor_role"],
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                now or utcnow(),
            ),
        )

    def append_revision(self, revision):
        with self._connect() as connection:
            self._insert_revision(connection, revision)

    @staticmethod
    def _revision_from_row(row):
        return {
            "seq": int(row["seq"]),
            "entity_id": row["entity_id"],
            "action": row["action"],
            "from_version": row["from_version"],
            "to_version": row["to_version"],
            "from_status": row["from_status"],
            "to_status": row["to_status"],
            "from_magnitude": row["from_magnitude"],
            "to_magnitude": row["to_magnitude"],
            "reason": row["reason"],
            "actor_id": row["actor_id"],
            "actor_role": row["actor_role"],
            "detail": json.loads(row["detail"]),
            "created_at": row["created_at"],
        }

    def list_revisions(self, entity_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM event_revisions WHERE entity_id = ? ORDER BY seq",
                (entity_id,),
            ).fetchall()
        return [self._revision_from_row(row) for row in rows]

    def ensure_event_baseline(self, entity, actor, reason=None):
        """Insert a first-view baseline for a legacy event that has no revisions.

        Returns the baseline row if it was inserted, otherwise None. The check
        and insert run in one locked transaction so concurrent views cannot add
        duplicate baselines.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            entity_id = entity["id"]
            existing = connection.execute(
                "SELECT seq FROM event_revisions WHERE entity_id = ? ORDER BY seq LIMIT 1",
                (entity_id,),
            ).fetchone()
            if existing:
                connection.commit()
                return None
            magnitude = entity["data"].get("magnitude")
            revision = {
                "entity_id": entity_id,
                "action": "first_view",
                "from_version": None,
                "to_version": int(entity["version"]),
                "from_status": None,
                "to_status": entity["status"],
                "from_magnitude": None,
                "to_magnitude": None if magnitude is None else str(magnitude),
                "reason": reason,
                "actor_id": actor.user_id,
                "actor_role": actor.role,
                "detail": {"baseline": True, "note": "legacy event first view"},
            }
            self._insert_revision(connection, revision)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.list_revisions(entity_id)[0]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
