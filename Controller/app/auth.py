"""Per-app authorization for the controller.

Two caller types reach the controller on different network paths:

- The admin UI calls over the internal service-to-service endpoint. SPCS injects
  no identity there, so the admin UI vouches for the operator's roles in the
  ``X-Operator-Roles`` header (it derived them from Snowflake under the operator's
  identity). Because the controller endpoint is public, that header is honoured only
  when the request also carries the shared ``X-Internal-Auth`` token both in-app
  services hold (``INTERNAL_AUTH_TOKEN`` env, set by setup_script.sql); otherwise a
  tokenless caller gets no roles.
- Machine clients (e.g. CI/CD pipelines) call the public endpoint with a PAT.
  SPCS injects ``Sf-Context-Current-User-Token`` (because the service runs with
  ``executeAsCaller: true``), which is Snowflake-asserted and cannot be forged.
  We resolve the caller's roles ourselves and ignore any ``X-Operator-Roles``.

The discriminator is the presence of the caller token. See the memory
``reference-spcs-ingress-identity-headers`` for why this is the correct boundary.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field

import snowflake.connector
from fastapi import Request

logger = logging.getLogger(__name__)

_CALLER_TOKEN_HEADER = "Sf-Context-Current-User-Token"
_OPERATOR_HEADER = "X-Operator"
_OPERATOR_ROLES_HEADER = "X-Operator-Roles"
_INTERNAL_AUTH_HEADER = "X-Internal-Auth"
_SERVICE_TOKEN_FILE = "/snowflake/session/token"

# Shared secret proving a request is the in-app admin UI on the internal hop (set
# as an env on both services by setup_script.sql). The controller endpoint is
# public, so the X-Operator-Roles header is only honoured when this token matches;
# without it (or on mismatch) a tokenless caller gets no roles. When unset (e.g. an
# install that predates this), the internal path stays trusted as before. An empty
# value is treated as unset so a mis-provision can't make an empty token match.
_INTERNAL_AUTH_TOKEN = os.environ.get("INTERNAL_AUTH_TOKEN") or None

# Caller identity (user + roles) is resolved from Snowflake on the public PAT
# path. Deploy clients poll status every ~10s, so without a cache each poll pays a
# full OAuth handshake; cache the resolved identity briefly, keyed by a hash of the
# caller token. TTL is short so freshly granted roles are picked up quickly.
_IDENTITY_CACHE_TTL_SECS = 60


@dataclass
class CallerIdentity:
    """The authenticated caller: Snowflake username (may be None on the internal
    path if no X-Operator header was sent) and the set of available roles."""
    user: str | None
    roles: set[str] = field(default_factory=set)


_cache_lock = threading.Lock()
_identity_cache: dict[str, tuple[float, CallerIdentity]] = {}


def _privileged_roles() -> frozenset[str]:
    raw = os.environ.get("PRIVILEGED_ROLES", "MENDIX_DEPLOY_CONTROLLER_ROLE")
    return frozenset(r.strip().upper() for r in raw.split(",") if r.strip())


# Roles that may act on any app regardless of owner_role (e.g. the deploy
# automation, whose single restricted role cannot be expressed per-app).
# Default is ACCOUNTADMIN (set in setup_script.sql): the consumer's break-glass
# admin already has full control of the app and its services, so treating it as the
# cross-app operator adds no privilege it lacks. A consumer wanting a narrower
# privileged operator can set PRIVILEGED_ROLES to a purpose-built account role.
PRIVILEGED_ROLES = _privileged_roles()


def _read_service_token() -> str:
    with open(_SERVICE_TOKEN_FILE) as f:
        return f.read().strip()


def _identity_from_snowflake(caller_token: str) -> CallerIdentity:
    """Open a short-lived caller-rights session and read the caller's user + roles.

    Caller-rights discipline: this session runs ONLY the identity query, opens and
    closes per call, and is never reused for any other query. It is separate from
    snowflake_client's owner-rights connection.
    """
    compound = f"{_read_service_token()}.{caller_token}"
    conn = snowflake.connector.connect(
        host=os.environ["SNOWFLAKE_HOST"],
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        token=compound,
        authenticator="oauth",
    )
    try:
        cur = conn.cursor()
        cur.execute("SELECT CURRENT_USER(), CURRENT_AVAILABLE_ROLES()")
        row = cur.fetchone()
        user = str(row[0]) if row and row[0] else None
        raw = row[1] if row and len(row) > 1 else None
        roles = json.loads(raw) if raw else []
        return CallerIdentity(user=user, roles={str(r).upper() for r in roles})
    finally:
        conn.close()


def _cached_identity(caller_token: str) -> CallerIdentity:
    key = hashlib.sha256(caller_token.encode()).hexdigest()
    now = time.monotonic()
    with _cache_lock:
        hit = _identity_cache.get(key)
        if hit and now - hit[0] < _IDENTITY_CACHE_TTL_SECS:
            return hit[1]
    # Resolve outside the lock (network call); a concurrent miss for the same
    # token at worst resolves twice, which is harmless.
    identity = _identity_from_snowflake(caller_token)
    with _cache_lock:
        # Opportunistically drop expired entries so the cache can't grow unbounded
        # as tokens rotate.
        expired = [k for k, (ts, _) in _identity_cache.items() if now - ts >= _IDENTITY_CACHE_TTL_SECS]
        for k in expired:
            del _identity_cache[k]
        _identity_cache[key] = (now, identity)
    return identity


def resolve_caller(request: Request) -> CallerIdentity:
    """Return the authoritative caller identity for the request (fail closed).

    Public PAT path: SPCS injects a caller token, so user + roles are resolved
    from Snowflake (and cached). Internal admin-UI path: trust the X-Operator /
    X-Operator-Roles headers the admin UI derived under the operator's identity.
    """
    caller_token = request.headers.get(_CALLER_TOKEN_HEADER)
    if caller_token:
        try:
            return _cached_identity(caller_token)
        except Exception:
            logger.exception("Failed to resolve caller identity from Snowflake")
            return CallerIdentity(user=None, roles=set())
    # No Snowflake caller token: this should only be the internal admin-UI hop.
    # The endpoint is public, so trust the vouched-for X-Operator-Roles only when the
    # request proves it is the in-app admin UI by presenting the shared internal token.
    if _INTERNAL_AUTH_TOKEN is None:
        # Fail closed: without a configured secret, X-Operator-Roles cannot be trusted.
        logger.warning("INTERNAL_AUTH_TOKEN not set; denying roles on the internal path")
        return CallerIdentity(user=None, roles=set())
    presented = request.headers.get(_INTERNAL_AUTH_HEADER) or ""
    if not hmac.compare_digest(presented, _INTERNAL_AUTH_TOKEN):
        logger.warning("Tokenless request without a valid internal auth token; denying roles")
        return CallerIdentity(user=None, roles=set())
    raw = request.headers.get(_OPERATOR_ROLES_HEADER, "")
    roles = {r.strip().upper() for r in raw.split(",") if r.strip()}
    return CallerIdentity(user=request.headers.get(_OPERATOR_HEADER) or None, roles=roles)


def resolve_caller_roles(request: Request) -> set[str]:
    """Return the authoritative role set for the request, or empty (fail closed)."""
    return resolve_caller(request).roles


def authorize(owner_role: str | None, roles: set[str]) -> bool:
    """True if the caller may act on an app owned by owner_role."""
    if roles & PRIVILEGED_ROLES:
        return True
    if not owner_role:
        return False
    return owner_role.upper() in roles
