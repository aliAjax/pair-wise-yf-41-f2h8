from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine

EVENT_KIND = "event"


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
        if kind == EVENT_KIND:
            revision = self._revision(
                entity_id=entity_id,
                actor=actor,
                action="create",
                from_version=None,
                to_version=1,
                from_status=None,
                to_status=status,
                from_magnitude=None,
                to_magnitude=payload.get("magnitude"),
                reason=payload.get("note") or "事件创建",
                detail={"kind": kind},
            )
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
        # Reject stale submissions up front so the stored record and its
        # revision history are never touched by an out-of-date edit.
        if expected != entity["version"]:
            raise ConflictError(
                "stale version: expected_version %s is out of date, "
                "current version is %s; refetch and resubmit"
                % (expected, entity["version"]),
                expected_version=expected,
                current_version=entity["version"],
            )
        payload = dict(data or {})
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, payload, self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        revision = None
        if entity["kind"] == EVENT_KIND:
            revision = self._revision(
                entity_id=entity_id,
                actor=actor,
                action=action,
                from_version=entity["version"],
                to_version=entity["version"] + 1,
                from_status=entity["status"],
                to_status=next_status,
                from_magnitude=entity["data"].get("magnitude"),
                to_magnitude=merged.get("magnitude"),
                reason=payload.get("reason") or payload.get("communication_id") or action,
                detail={"patch": patch},
            )
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

    @staticmethod
    def _revision(**kwargs):
        actor = kwargs.pop("actor")
        kwargs["actor_id"] = actor.user_id
        kwargs["actor_role"] = actor.role
        for key in ("from_magnitude", "to_magnitude"):
            value = kwargs.get(key)
            if value is not None:
                kwargs[key] = str(value)
        kwargs.setdefault("detail", {})
        return kwargs

    def get(self, entity_id, actor=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if actor is not None and entity["kind"] == EVENT_KIND:
            self.repository.ensure_event_baseline(entity, actor)
        return entity

    def revisions(self, entity_id, actor=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] != EVENT_KIND:
            raise ValidationError("revisions are only available for events")
        if actor is not None:
            self.repository.ensure_event_baseline(entity, actor)
        return self.repository.list_revisions(entity_id)

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
