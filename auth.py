"""auth.py — server-side session primitives and an RBAC-ready data model.

The existing deployment identifies anonymous browsers by a signed cookie
(``nova_sid``).  That is isolation, not identity.  This module keeps that
behaviour working while introducing the structures needed for real accounts:
User / Session / Workspace / Organization / Membership / Role, ownership
checks, session rotation and "log out everywhere".

Compatibility: sessions created before this module have no ``admin_epoch``.
They remain valid while ``ADMIN_LEGACY_SESSIONS=1`` (default).  Production
deployments must set it to ``0`` so that a revoked epoch invalidates every
administrator cookie.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from flask import session as flask_session

_admin_epoch = secrets.token_hex(16)


class Role(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"
    GUEST = "guest"


@dataclass(frozen=True)
class User:
    id: str
    email: str = ""
    display_name: str = ""
    mfa_enabled: bool = False


@dataclass(frozen=True)
class Organization:
    id: str
    name: str


@dataclass(frozen=True)
class Membership:
    user_id: str
    organization_id: str
    role: Role = Role.MEMBER


@dataclass
class Workspace:
    id: str
    owner_id: str
    organization_id: str = ""
    root: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class Session:
    id: str
    user_id: str
    created_at: float = field(default_factory=time.time)
    last_seen_at: float = 0.0
    revoked: bool = False
    mfa_verified: bool = False


def is_admin(sess: Any = None) -> bool:
    """Server-side authorization check for administrator sessions."""
    s = flask_session if sess is None else sess
    if not s or not s.get("admin_logged_in"):
        return False
    epoch = s.get("admin_epoch")
    if epoch == _admin_epoch:
        return True
    if epoch is None:
        import os
        return os.getenv("ADMIN_LEGACY_SESSIONS", "1") == "1"
    return False


def grant_admin(sess: Any = None, username: str = "admin") -> None:
    s = flask_session if sess is None else sess
    s["admin_logged_in"] = True
    s["admin_username"] = username
    s["admin_epoch"] = _admin_epoch


def revoke_admin(sess: Any = None) -> None:
    s = flask_session if sess is None else sess
    for key in ("admin_logged_in", "admin_username", "admin_epoch"):
        s.pop(key, None)


def logout_everywhere() -> str:
    """Invalidate every session carrying an admin epoch. Returns the new epoch."""
    global _admin_epoch
    _admin_epoch = secrets.token_hex(16)
    return _admin_epoch


def rotate_session(sess: Any = None) -> str:
    """Rotate the authentication version without losing chat ownership.

    ``nova_sid`` deliberately stays: it is the stable owner key for existing
    chats.  A full identity migration must remap chat owners in the database.
    """
    s = flask_session if sess is None else sess
    s["auth_version"] = secrets.token_hex(16)
    s["rotated_at"] = time.time()
    return s["auth_version"]


def can_access(resource_owner: str, user_owner: str, role: Role = Role.GUEST) -> bool:
    """Owner/admin access check; never rely on client-provided flags."""
    if not resource_owner or not user_owner:
        return False
    if resource_owner == user_owner:
        return True
    return role in (Role.OWNER, Role.ADMIN)
