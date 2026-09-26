from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine

LEGACY_BASELINE_REASON = "旧事件首次查看，自动建立修订基线"


def _magnitude_of(data):
    """Return a numeric magnitude or None. Invalid values stay out of the trail."""
    value = (data or {}).get("magnitude")
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        revision = None
        if kind == "event":
            revision = {
                "action": "create",
                "from_version": None,
                "from_status": None,
                "to_status": status,
                "from_magnitude": None,
                "to_magnitude": _magnitude_of(payload),
                "reason": payload.get("reason") or "事件创建",
                "actor_id": actor.user_id,
                "actor_role": actor.role,
                "snapshot": payload,
            }
        entity = self.repository.create_entity(
            entity_id, kind, status, payload, actor.user_id, revision=revision
        )
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        revision = None
        if entity["kind"] == "event":
            revision = {
                "action": action,
                "from_version": entity["version"],
                "from_status": entity["status"],
                "to_status": next_status,
                "from_magnitude": _magnitude_of(entity["data"]),
                "to_magnitude": _magnitude_of(merged),
                "reason": merged.get("reason") or patch.get("reason"),
                "actor_id": actor.user_id,
                "actor_role": actor.role,
                "snapshot": merged,
            }
        updated = self.repository.update_entity(
            entity_id, expected, next_status, merged, revision=revision
        )
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id, actor=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "event":
            # Events created before revision tracking existed get a baseline
            # entry the first time anyone opens them.
            self.repository.ensure_legacy_event_baseline(
                entity_id,
                actor.user_id if actor else "anonymous",
                actor.role if actor else "viewer",
                LEGACY_BASELINE_REASON,
            )
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    def event_revisions(self, entity_id, actor=None):
        entity = self.get(entity_id, actor=actor)
        if entity["kind"] != "event":
            raise ValidationError("revisions are only available for events")
        return self.repository.list_event_revisions(entity_id)

    def event_revision(self, entity_id, seq, actor=None):
        entity = self.get(entity_id, actor=actor)
        if entity["kind"] != "event":
            raise ValidationError("revisions are only available for events")
        revision = self.repository.get_event_revision(entity_id, int(seq))
        if not revision:
            raise NotFoundError(
                "revision %s not found for event %s" % (seq, entity_id)
            )
        return revision
