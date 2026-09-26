from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class DomainError(Exception):
    """Base error for domain failures."""


class ValidationError(DomainError):
    """Input does not satisfy a domain rule."""


class PermissionDenied(DomainError):
    """Actor is not allowed to perform the action."""


class NotFoundError(DomainError):
    """Requested record does not exist."""


class ConflictError(DomainError):
    """A version or uniqueness constraint was violated."""


class StaleVersionError(ConflictError):
    """Submitter based the change on an out-of-date expected_version."""

    def __init__(self, expected_version, current_version):
        self.expected_version = int(expected_version)
        self.current_version = int(current_version)
        if self.expected_version < self.current_version:
            message = (
                "stale version: expected_version %s is out of date, current version is %s; "
                "reload the latest record and resubmit"
                % (self.expected_version, self.current_version)
            )
        else:
            message = (
                "version conflict: expected_version %s does not exist, current version is %s"
                % (self.expected_version, self.current_version)
            )
        super().__init__(message)


class InvalidTransition(DomainError):
    """The requested state transition is not valid."""


class Role(str, Enum):
    viewer = "viewer"
    admin = "admin"
    analyst = "analyst"
    reviewer = "reviewer"
    station = "station"


@dataclass
class Actor:
    user_id: str
    role: str

    @classmethod
    def from_headers(cls, headers):
        user_id = headers.get("X-User-Id", "anonymous")
        role = headers.get("X-Role", "viewer")
        if role not in {item.value for item in Role}:
            raise PermissionDenied("unknown role: " + role)
        return cls(user_id=user_id, role=role)


@dataclass
class Entity:
    id: str
    kind: str
    status: str
    version: int
    data: Dict[str, Any]
    created_by: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"],
            kind=row["kind"],
            status=row["status"],
            version=row["version"],
            data=row["data"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
